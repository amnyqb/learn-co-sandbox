"""Local HLS downloader and converter. Run with python3 app.py."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urljoin, urlsplit
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from downloader import AccessError, CompatibilityError, HLSDownloader, MAX_CONNECTIONS, configure_file_limit, storage_error
from relay import FFmpegRelay

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("VID_DOWN_DATA", str(ROOT / ".local"))).resolve()
THREADS = max(1, min(4, (os.cpu_count() or 2) - 1))
TERMINAL = {"completed", "failed", "cancelled"}


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=8192)
    name: str = Field(default="my-video", max_length=120)
    format: str = Field(default="mp4", pattern="^(mp4|hevc|webm)$")
    preset: str = Field(default="balanced", pattern="^(small|balanced|original)$")
    target_mb: float | None = Field(default=None, ge=0.1, le=100000)
    referer: str = Field(default="", max_length=2048)
    connections: int = Field(default=MAX_CONNECTIONS, ge=1, le=MAX_CONNECTIONS)
    engine: str = Field(default="auto", pattern="^(auto|parallel|ffmpeg)$")
    retries: int = Field(default=4, ge=0, le=8)
    user_agent: str = Field(default="vid_down/1.1", min_length=1, max_length=1024)
    cookies: str = Field(default="", max_length=8192, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    fallback_urls: list[str] = Field(default_factory=list, max_length=5, repr=False)

    @field_validator("cookies", "user_agent")
    @classmethod
    def header_value(cls, value):
        if any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("Request headers must use printable ASCII without line breaks.")
        return value.strip()

    @field_validator("headers")
    @classmethod
    def custom_headers(cls, value):
        if len(value) > 24 or sum(len(k) + len(v) for k, v in value.items()) > 16384:
            raise ValueError("Use at most 24 custom headers and 16 KB of header text.")
        blocked = {"host", "connection", "content-length", "transfer-encoding", "range", "proxy-authorization", "cookie"}
        for name, contents in value.items():
            if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", name) or name.lower() in blocked:
                raise ValueError("Use ordinary request header names. Enter cookies in the Cookie field.")
            cls.header_value(contents)
        return value

    @field_validator("fallback_urls")
    @classmethod
    def alternative_urls(cls, values):
        result = []
        for value in values:
            if not value.strip() or len(value) > 8192:
                raise ValueError("Each alternative stream needs a complete HTTP or HTTPS URL.")
            result.append(cls.http_url(value))
        return result

    @field_validator("url", "referer")
    @classmethod
    def http_url(cls, value):
        value = value.strip()
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Use a complete HTTP or HTTPS URL.")
        if parsed.username or parsed.password or any(ord(c) < 32 for c in value):
            raise ValueError("URL credentials and control characters are not supported.")
        return value

    @model_validator(mode="after")
    def compatible_options(self):
        if not self.url:
            raise ValueError("Enter a stream URL.")
        if self.preset == "original" and (self.format != "mp4" or self.target_mb is not None):
            raise ValueError("Keep original uses MP4 without a target size. Choose a compression preset for other options.")
        return self


def clean_name(value):
    value = re.sub(r"\.(mp4|webm|mkv)$", "", value.strip(), flags=re.I)
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")[:100] or "my-video"


async def resolve_playlist(url, options, depth=0):
    """Resolve master playlists to their highest-bandwidth finite video variant."""
    if depth > 5:
        raise ValueError("The playlist contains too many nested variants.")
    DownloadRequest.http_url(url)
    transport = HLSDownloader(options, DATA)
    async with httpx.AsyncClient(timeout=20) as client:
        transport.client = client
        text, resolved_url = await transport.transfer(url)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("This URL did not return an M3U8 playlist.")
    variants = []
    for index, line in enumerate(lines):
        references = re.findall(r'URI="([^"]+)"', line) if line.startswith("#") else [line]
        for reference in references:
            if urlsplit(urljoin(resolved_url, reference)).scheme not in {"http", "https"}:
                raise ValueError("The playlist contains an unsupported segment or key protocol.")
        if line.startswith("#EXT-X-STREAM-INF:"):
            bandwidth = re.search(r"(?:[:,])BANDWIDTH=(\d+)", line)
            uri = next((candidate for candidate in lines[index + 1:] if not candidate.startswith("#")), None)
            if uri:
                variants.append((int(bandwidth[1]) if bandwidth else 0, urljoin(resolved_url, uri)))
        if line.startswith("#EXT-X-KEY:") and ("SAMPLE-AES" in line or ("KEYFORMAT=" in line and 'KEYFORMAT="identity"' not in line)):
            raise ValueError("This stream uses unsupported DRM. Use an accessible, non-DRM stream.")
    if variants:
        # Let FFmpeg process the master so separate EXT-X-MEDIA audio is retained.
        # Validate the selected video variant to avoid indefinitely recording live HLS.
        await resolve_playlist(max(variants)[1], options, depth + 1)
        return resolved_url
    if "#EXT-X-ENDLIST" not in lines:
        raise ValueError("This is a live stream. This version downloads completed, on-demand playlists.")
    return resolved_url


def encode_args(options, duration, has_audio, work_dir):
    codec = {"mp4": "libx264", "hevc": "libx265", "webm": "libvpx-vp9"}[options.format]
    args = ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c:v", codec, "-threads", str(THREADS)]
    height = 720 if options.preset == "small" else 1080
    # Divisible by two, with no upscaling and no forced aspect ratio.
    args += ["-vf", f"scale=w=-2:h='min({height},trunc(ih/2)*2)'", "-pix_fmt", "yuv420p"]
    if options.format == "webm":
        args += ["-deadline", "good", "-cpu-used", "3", "-row-mt", "1"]
    else:
        args += ["-preset", "medium"]
    if options.format == "hevc":
        args += ["-tag:v", "hvc1", "-x265-params", f"pools={THREADS}:frame-threads=1:log-level=error"]
    audio_bps = 64000 if options.preset == "small" else 96000
    args += ["-c:a", "libopus" if options.format == "webm" else "aac", "-b:a", str(audio_bps)]
    if options.target_mb is not None:
        if not duration or duration <= 0:
            raise ValueError("Target size needs a known video duration.")
        video_bps = math.floor(options.target_mb * 1_000_000 * 8 * 0.96 / duration) - (audio_bps if has_audio else 0)
        if video_bps < 32000:
            raise ValueError("That target is too small for this duration. Increase the target size.")
        args += ["-b:v", str(video_bps)]
        if options.format != "hevc":
            args += ["-passlogfile", str(work_dir / "pass")]
    else:
        crf = {"mp4": (28, 23), "hevc": (30, 25), "webm": (38, 32)}[options.format][options.preset != "small"]
        args += ["-crf", str(crf)]
        if options.format == "webm":
            args += ["-b:v", "0"]
    return args


class JobManager:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(2)
        for path in DATA.glob("*/job.json"):
            try:
                job = json.loads(path.read_text())
                if job["status"] not in TERMINAL:
                    job.update(status="failed", stage="Interrupted", error="The app stopped before this job finished. Add the stream again.")
                    self.save(job)
                    self.clean_work(job["id"])
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                continue

    def save(self, job):
        directory = DATA / job["id"]
        directory.mkdir(exist_ok=True)
        tmp = directory / "job.json.tmp"
        tmp.write_text(json.dumps(job))
        tmp.replace(directory / "job.json")

    def update(self, job, **fields):
        job.update(fields)
        self.save(job)

    def clean_work(self, job_id):
        shutil.rmtree(DATA / job_id / "segments", ignore_errors=True)
        for pattern in ("source.mkv", "pass*", "x265*", "*.partial.*"):
            for path in (DATA / job_id).glob(pattern):
                if path.is_file():
                    path.unlink(missing_ok=True)

    def add(self, options):
        if sum(job["status"] not in TERMINAL for job in self.jobs.values()) >= 12:
            raise HTTPException(429, "The queue is full. Wait for a download to finish.")
        job_id = uuid.uuid4().hex
        extension = "webm" if options.format == "webm" else "mp4"
        job = dict(id=job_id, name=clean_name(options.name), filename=f"{clean_name(options.name)}.{extension}",
                   format=options.format, preset=options.preset, target_mb=options.target_mb,
                   source_host=urlsplit(options.url).hostname, created_at=datetime.now(timezone.utc).isoformat(),
                   status="queued", stage="Queued", progress=0, duration=None,
                   source_bytes=None, output_bytes=None, width=None, height=None, error=None,
                   requested_connections=options.connections, connections=0, engine_used=None,
                   download_bytes=0, download_speed=0, downloaded_segments=0, total_segments=0,
                   source_attempts=[], fallback_reason=None)
        self.jobs[job_id] = job
        self.save(job)
        task = asyncio.create_task(self.run(job, options))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return job

    async def process(self, args, job, start=0, weight=0, probe=False, loopback=False):
        environment = None
        if loopback:
            # Upstream traffic is still proxied by HTTPX; only FFmpeg's own
            # job-scoped loopback hop bypasses outbound proxies.
            environment = os.environ.copy()
            for name in list(environment):
                if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                    del environment[name]
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment)
        errors = deque(maxlen=15)

        async def read_errors():
            async for line in process.stderr:
                errors.append(line.decode(errors="replace").strip())

        reader = asyncio.create_task(read_errors())
        try:
            if probe:
                output = await asyncio.wait_for(process.stdout.read(), timeout=40)
            else:
                output = b""
                async for line in process.stdout:
                    text = line.decode(errors="replace").strip()
                    if text.startswith("out_time_us=") and job.get("duration"):
                        try:
                            fraction = int(text.split("=", 1)[1]) / 1_000_000 / job["duration"]
                            self.update(job, progress=round(min(99, start + min(1, max(0, fraction)) * weight), 1))
                        except ValueError:
                            pass
                    if shutil.disk_usage(DATA).free < 100_000_000:
                        raise ValueError("Disk space is running low. Remove an old file and try again.")
            await process.wait()
            await reader
            if process.returncode:
                # URLs may contain credentials in query parameters; keep them out of UI errors.
                detail = " ".join(errors)
                if "403" in detail or "401" in detail:
                    raise AccessError("The stream denied access. Check the signed URL, Referer, and session cookies/headers.")
                if "404" in detail:
                    raise ValueError("A playlist or video segment was not found. Check whether the URL expired.")
                if "Protocol" in detail and "whitelist" in detail:
                    raise ValueError("The playlist contains an unsupported segment or key protocol.")
                if "codec" in detail.lower() and job["preset"] == "original":
                    raise ValueError("The source codecs cannot be kept in MP4. Choose Balanced to convert them.")
                raise ValueError("FFmpeg could not process this stream. Check that all segments are available and the stream is not DRM-protected.")
            return output
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def ffmpeg_download(self, job, options, url, source):
        url = await resolve_playlist(url, options)
        self.update(job, engine_used="ffmpeg", connections=1, stage="Downloading with FFmpeg")
        async with FFmpegRelay(options, source.parent / "segments") as relay:
            local_url = relay.register(url, "playlist")
            try:
                await self.ffmpeg_relay_download(job, relay, local_url, source)
            except (ValueError, asyncio.TimeoutError):
                if relay.error:
                    raise AccessError(relay.error) from None
                raise

    async def ffmpeg_relay_download(self, job, relay, local_url, source):
        args = ["-protocol_whitelist", "http,tcp,crypto,data", "-rw_timeout", "30000000", "-allowed_extensions", "ALL", "-http_multiple", "0"]
        raw = await self.process(["ffprobe", "-v", "error", *args, "-show_format", "-show_streams", "-show_programs", "-of", "json", local_url], job, probe=True, loopback=True)
        metadata = json.loads(raw)
        videos = [s for s in metadata["streams"] if s["codec_type"] == "video"]
        if not videos:
            raise AccessError("The playlist has no video track.")
        video = max(videos, key=lambda s: s.get("width", 0) * s.get("height", 0))
        audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
        duration = float(metadata.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise AccessError("The playlist does not report a finite duration.")
        self.update(job, duration=duration, width=video.get("width"), height=video.get("height"))
        maps = ["-map", f"0:{video['index']}"]
        program = next((p for p in metadata.get("programs", []) if any(s.get("index") == video["index"] for s in p.get("streams", []))), None)
        selected_audio = next((s for s in (program or {}).get("streams", []) if s.get("codec_type") == "audio"), audio[0] if audio else None)
        if selected_audio:
            maps += ["-map", f"0:{selected_audio['index']}"]
        await self.process(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args, "-i", local_url,
                            *maps, "-c", "copy", "-sn", "-dn", "-progress", "pipe:1", str(source)], job, 0, 35, loopback=True)

    async def download_source(self, job, options, source):
        attempts = []
        for index, url in enumerate([options.url, *options.fallback_urls]):
            self.clean_work(job["id"])
            self.update(job, stage="Reading playlist" if index == 0 else "Trying alternative stream", progress=0,
                        source_host=urlsplit(url).hostname, fallback_reason=None,
                        download_bytes=0, download_speed=0, downloaded_segments=0, total_segments=0)
            try:
                if options.engine == "ffmpeg":
                    await self.ffmpeg_download(job, options, url, source)
                else:
                    transport = HLSDownloader(options, source.parent / "segments", lambda **fields: self.update(job, **fields))
                    try:
                        playlist = await transport.download(url)
                    except CompatibilityError as error:
                        if options.engine != "auto":
                            raise AccessError("This playlist needs FFmpeg compatibility mode. Select Automatic or FFmpeg, or use a direct media playlist.") from None
                        self.update(job, fallback_reason=str(error), stage="Trying FFmpeg compatibility mode")
                        await self.ffmpeg_download(job, options, url, source)
                    else:
                        self.update(job, stage="Assembling downloaded segments", progress=30)
                        try:
                            await self.process(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                                                "-protocol_whitelist", "file,crypto,data", "-allowed_extensions", "ALL", "-i", str(playlist),
                                                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn", "-progress", "pipe:1", str(source)], job, 30, 5)
                        except ValueError:
                            if options.engine != "auto":
                                raise
                            self.update(job, fallback_reason="The cached playlist needed native FFmpeg transport.", stage="Trying FFmpeg compatibility mode")
                            self.clean_work(job["id"])
                            await self.ffmpeg_download(job, options, url, source)
                shutil.rmtree(source.parent / "segments", ignore_errors=True)
                self.update(job, source_attempts=attempts)
                return
            except (ValueError, httpx.HTTPError) as error:
                message = str(error) if isinstance(error, ValueError) else "The stream could not be reached."
                attempts.append({"host": urlsplit(url).hostname, "error": message})
                self.update(job, source_attempts=attempts)
                if index == len(options.fallback_urls):
                    raise

    async def run(self, job, options):
        directory = DATA / job["id"]
        source = directory / "source.mkv"
        partial = directory / ("output.partial.webm" if options.format == "webm" else "output.partial.mp4")
        try:
            async with self.semaphore:
                if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                    raise ValueError("FFmpeg is missing. Install FFmpeg and restart the app.")
                self.update(job, status="running", stage="Reading playlist")
                await self.download_source(job, options, source)
                # Probe the downloaded copy: HLS timestamps can differ from the manifest.
                local = json.loads(await self.process(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)], job, probe=True))
                duration = float(local["format"]["duration"])
                has_audio = any(s["codec_type"] == "audio" for s in local["streams"])
                self.update(job, duration=duration, source_bytes=source.stat().st_size, stage="Converting video", progress=35)
                base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
                if options.preset == "original":
                    await self.process([*base, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", str(partial)], job, 35, 64)
                else:
                    encoder = encode_args(options, duration, has_audio, directory)
                    faststart = [] if options.format == "webm" else ["-movflags", "+faststart"]
                    if options.target_mb:
                        if options.format == "hevc":
                            def pass_options(number):
                                settings = encoder.copy()
                                position = settings.index("-x265-params") + 1
                                settings[position] += f":pass={number}:stats={directory / 'x265.log'}"
                                return settings
                            first, second = pass_options(1), pass_options(2)
                        else:
                            first, second = [*encoder, "-pass", "1"], [*encoder, "-pass", "2"]
                        self.update(job, stage="Analyzing video · pass 1 of 2")
                        await self.process([*base, *first, "-an", "-progress", "pipe:1", "-f", "null", os.devnull], job, 35, 30)
                        self.update(job, stage="Converting video · pass 2 of 2")
                        await self.process([*base, *second, *faststart, "-progress", "pipe:1", str(partial)], job, 65, 34)
                        # Very short VP9 videos can overshoot their average bitrate.
                        # Reuse the analysis and lower the video budget if needed.
                        target_bytes = options.target_mb * 1_000_000
                        for attempt in range(3):
                            if partial.stat().st_size <= target_bytes:
                                break
                            position = second.index("-b:v") + 1
                            bitrate = math.floor(int(second[position]) * target_bytes / partial.stat().st_size * 0.93)
                            if bitrate < 32000:
                                raise ValueError("That target is too small for this video. Increase the target size.")
                            second[position] = str(bitrate)
                            self.update(job, stage="Refining target file size")
                            await self.process([*base, *second, *faststart, "-progress", "pipe:1", str(partial)], job, 97, 2)
                    else:
                        await self.process([*base, *encoder, *faststart, "-progress", "pipe:1", str(partial)], job, 35, 64)
                result = json.loads(await self.process(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(partial)], job, probe=True))
                out_video = next(s for s in result["streams"] if s["codec_type"] == "video")
                partial.replace(directory / job["filename"])
                self.update(job, status="completed", stage="Ready", progress=100, output_bytes=(directory / job["filename"]).stat().st_size,
                            width=out_video["width"], height=out_video["height"], duration=float(result["format"]["duration"]))
        except asyncio.CancelledError:
            self.update(job, status="cancelled", stage="Cancelled", error=None)
        except (ValueError, KeyError, OSError, httpx.HTTPError, asyncio.TimeoutError) as error:
            if isinstance(error, OSError):
                message = storage_error(error)
            elif isinstance(error, httpx.HTTPStatusError):
                message = f"The playlist returned HTTP {error.response.status_code}. Check its URL, expiry, and access requirements."
            elif isinstance(error, httpx.HTTPError):
                message = "Could not reach the playlist. Check the URL and this machine's network access."
            elif isinstance(error, asyncio.TimeoutError):
                message = "Reading the stream timed out. Check the source and try again."
            elif isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
                message = str(error)
            else:
                message = "The video could not be processed. Check the stream and available disk space."
            self.update(job, status="failed", stage="Failed", error=message)
        except Exception:
            self.update(job, status="failed", stage="Failed", error="An unexpected processing error occurred. Add the stream again.")
        finally:
            self.clean_work(job["id"])

    async def cancel(self, job_id):
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        job = self.jobs[job_id]
        if job["status"] not in TERMINAL:
            self.update(job, status="cancelled", stage="Cancelled")
        self.clean_work(job_id)
        return job


manager = None


@asynccontextmanager
async def lifespan(app):
    global manager
    manager = JobManager()
    yield
    await asyncio.gather(*(manager.cancel(job_id) for job_id in list(manager.tasks)), return_exceptions=True)


app = FastAPI(title="vid_down", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])


@app.middleware("http")
async def local_access(request: Request, call_next):
    if request.method in {"POST", "DELETE", "PATCH", "PUT"}:
        origin = request.headers.get("origin")
        if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Open the app directly to change downloads."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_job(job_id):
    if job_id not in manager.jobs:
        raise HTTPException(404, "Download not found.")
    return manager.jobs[job_id]


@app.get("/api/health")
async def health():
    return {"ok": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "ffmpeg": bool(shutil.which("ffmpeg")), "free_bytes": shutil.disk_usage(DATA).free}


@app.get("/api/jobs")
async def jobs():
    return sorted(manager.jobs.values(), key=lambda j: j["created_at"], reverse=True)


@app.post("/api/jobs", status_code=202)
async def create_job(options: DownloadRequest):
    return manager.add(options)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    get_job(job_id)
    return await manager.cancel(job_id)


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str):
    job = get_job(job_id)
    if job["status"] not in TERMINAL:
        raise HTTPException(409, "Cancel this download before removing it.")
    shutil.rmtree(DATA / job_id)
    del manager.jobs[job_id]
    return {"ok": True}


@app.get("/api/jobs/{job_id}/file")
async def download_file(job_id: str, preview: bool = False):
    job = get_job(job_id)
    path = DATA / job_id / job["filename"]
    if job["status"] != "completed" or not path.is_file():
        raise HTTPException(409, "This file is not ready yet.")
    return FileResponse(path, media_type="video/webm" if job["format"] == "webm" else "video/mp4",
                        filename=job["filename"], content_disposition_type="inline" if preview else "attachment")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    configure_file_limit()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("FFmpeg is required. Install it with your system package manager before starting vid_down.")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("VID_DOWN_PORT", "8000")))
