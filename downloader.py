"""Concurrent HLS transport; FFmpeg remains responsible for demuxing/decryption."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urljoin, urlsplit

import httpx

MAX_CONNECTIONS = 128
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}


class AccessError(ValueError):
    """A safe, actionable message that excludes signed URLs and credentials."""


class CompatibilityError(ValueError):
    """An HLS feature for which native FFmpeg transport is appropriate."""


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def checked_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise AccessError("The playlist contains an unsupported segment or key protocol.")
    if any(ord(char) < 32 for char in url):
        raise AccessError("The playlist contains an invalid URL.")
    if "{$" in url:
        raise CompatibilityError("This playlist uses HLS variables; trying FFmpeg compatibility mode.")
    return url


def attributes(line):
    return dict((match[1], match[2].strip('"')) for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[-1]))


def retry_delay(value, attempt):
    if value:
        try:
            return min(120, max(0, float(value)))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return min(120, max(0, (when - datetime.now(timezone.utc)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass
    return min(10, .4 * 2 ** attempt)


class AdaptiveWindow:
    def __init__(self, limit):
        self.limit = limit
        self.maximum = limit
        self.successes = 0
        self.active = 0
        self.not_before = 0
        self.condition = asyncio.Condition()

    async def acquire(self):
        async with self.condition:
            while self.active >= self.limit or time.monotonic() < self.not_before:
                delay = self.not_before - time.monotonic()
                try:
                    if delay > 0:
                        await asyncio.wait_for(self.condition.wait(), delay)
                    else:
                        await self.condition.wait()
                except asyncio.TimeoutError:
                    pass
            self.active += 1

    async def release(self):
        async with self.condition:
            self.active -= 1
            self.condition.notify_all()

    async def throttle(self, delay):
        async with self.condition:
            self.limit = max(1, self.limit // 2)
            self.successes = 0
            self.not_before = max(self.not_before, time.monotonic() + delay)
            self.condition.notify_all()

    async def success(self):
        async with self.condition:
            self.successes += 1
            if self.limit < self.maximum and self.successes >= max(8, self.limit * 2) and time.monotonic() > self.not_before + 1:
                self.limit += 1
                self.successes = 0
                self.condition.notify_all()


@dataclass
class Asset:
    url: str
    path: Path
    byte_range: tuple[int, int] | None = None
    is_key: bool = False


class HLSDownloader:
    def __init__(self, options, directory, report=lambda **fields: None):
        self.options = options
        self.directory = Path(directory)
        self.report = report
        self.assets = []
        self.asset_index = {}
        self.media_index = 0
        self.duration = 0
        self.bytes_downloaded = 0
        self.completed = 0
        self.started = time.monotonic()
        self.last_report = 0
        self.window = AdaptiveWindow(options.connections)
        self.client = None

    def headers(self, url):
        headers = {"User-Agent": self.options.user_agent}
        if self.options.referer:
            headers["Referer"] = self.options.referer
        # Credentials are scoped to the primary URL's origin, including its port.
        # HTTPX strips Authorization and Cookie on cross-origin redirects too.
        if origin(url) == origin(self.options.url):
            headers.update(self.options.headers)
            if self.options.cookies:
                headers["Cookie"] = self.options.cookies
        return headers

    def progress(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_report < .15:
            return
        self.last_report = now
        self.report(
            stage=f"Downloading segments · {self.completed}/{len(self.assets)} files · {self.window.limit} connections",
            progress=round(30 * self.completed / max(1, len(self.assets)), 1),
            download_bytes=self.bytes_downloaded,
            download_speed=round(self.bytes_downloaded / max(.1, now - self.started)),
            connections=self.window.limit, downloaded_segments=self.completed, total_segments=len(self.assets),
        )

    def asset(self, url, kind="segment", byte_range=None):
        checked_url(url)
        key = (url, byte_range, kind == "key")
        if key not in self.asset_index:
            suffix = Path(urlsplit(url).path).suffix.lower()
            extension = ".key" if kind == "key" else suffix if suffix in {".ts", ".m4s", ".mp4", ".aac", ".vtt"} else ".ts"
            path = self.directory / f"asset-{len(self.assets):06d}{extension}"
            self.assets.append(Asset(url, path, byte_range, kind == "key"))
            self.asset_index[key] = path.name
        return self.asset_index[key]

    @asynccontextmanager
    async def response(self, url, byte_range=None):
        # Rebuild headers on every redirect: custom authentication headers must
        # never leak to a CDN or redirect destination on a different origin.
        for _ in range(11):
            checked_url(url)
            headers = self.headers(url)
            if byte_range:
                headers["Accept-Encoding"] = "identity"
                start, length = byte_range
                headers["Range"] = f"bytes={start}-{start + length - 1}"
            async with self.client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise AccessError("The stream returned a redirect without a destination.")
                    url = urljoin(str(response.url), location)
                    continue
                yield response
                return
        raise AccessError("The stream returned too many redirects.")

    async def transfer(self, url, path=None, byte_range=None, is_key=False):
        checked_url(url)
        for attempt in range(self.options.retries + 1):
            part = path.with_suffix(path.suffix + ".part") if path else None
            await self.window.acquire()
            delay = None
            try:
                async with self.response(url, byte_range) as response:
                    status = response.status_code
                    host = response.url.host
                    if status in RETRY_CODES:
                        if attempt == self.options.retries:
                            raise AccessError(f"HTTP {status} from {host} after retries. Reduce connections or try again later.")
                        delay = retry_delay(response.headers.get("retry-after"), attempt)
                        if status == 429:
                            await self.window.throttle(delay)
                    elif status >= 400:
                        if status in {401, 403}:
                            raise AccessError(f"HTTP {status} from {host}. Use a current signed URL, the required Referer, or your session cookies/headers. A different downloader cannot grant access.")
                        raise AccessError(f"HTTP {status} from {host}. Check the URL or supply an alternative stream URL.")
                    elif byte_range is None and status != 200:
                        raise AccessError(f"Unexpected HTTP {status} from {host}; the server returned an incomplete resource.")
                    elif byte_range is not None and status not in {200, 206}:
                        raise AccessError(f"The server could not provide the requested video byte range on {host}.")
                    else:
                        # Even when a server ignores Range, materialize exactly the
                        # requested bytes. Never concatenate a whole resource twice.
                        skip = byte_range[0] if byte_range and status == 200 else 0
                        wanted = byte_range[1] if byte_range else None
                        if byte_range and status == 206:
                            expected = f"bytes {byte_range[0]}-{byte_range[0] + byte_range[1] - 1}/"
                            if not response.headers.get("content-range", "").startswith(expected):
                                raise AccessError(f"The server returned the wrong video byte range on {host}.")
                        body = bytearray()
                        written = 0
                        handle = part.open("wb") if part else None
                        try:
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                if skip:
                                    cut = min(skip, len(chunk))
                                    chunk = chunk[cut:]
                                    skip -= cut
                                if wanted is not None:
                                    chunk = chunk[:wanted - written]
                                if handle:
                                    handle.write(chunk)
                                    self.bytes_downloaded += len(chunk)
                                    self.progress()
                                    if shutil.disk_usage(self.directory).free < 100_000_000:
                                        raise AccessError("Disk space is running low. Remove an old file and try again.")
                                else:
                                    body.extend(chunk)
                                    if len(body) > 2_000_000:
                                        raise AccessError("The playlist is too large. Enter an M3U8 playlist URL.")
                                written += len(chunk)
                                if is_key and written > 16:
                                    raise AccessError("An AES-128 key did not contain exactly 16 bytes.")
                                if wanted is not None and written == wanted:
                                    break
                        finally:
                            if handle:
                                handle.close()
                        if not written or (wanted is not None and written != wanted) or (is_key and written != 16):
                            raise httpx.ReadError("Incomplete HLS resource")
                        if part:
                            part.replace(path)
                            await self.window.success()
                            return str(response.url)
                        return bytes(body).decode("utf-8-sig"), str(response.url)
            except (httpx.HTTPError, OSError) as error:
                if isinstance(error, OSError):
                    raise AccessError("Could not write video segments. Check free disk space and directory permissions.") from None
                if "CERTIFICATE_VERIFY_FAILED" in str(error) or "certificate verify failed" in str(error).lower():
                    raise AccessError(f"TLS certificate verification failed for {urlsplit(url).hostname}. Fix the server certificate or trusted CA configuration; verification stays enabled.") from None
                if isinstance(error, httpx.ProxyError) and re.search(r"\b(403|407)\b", str(error)):
                    raise AccessError(f"The configured proxy denied the connection to {urlsplit(url).hostname}. Check proxy authentication and the cloud domain allowlist.") from None
                if attempt == self.options.retries:
                    raise AccessError(f"Could not reach {urlsplit(url).hostname} after retries. Check network/domain access, your configured proxy, and the URL. Try an alternative URL or run on your own computer if cloud access is restricted.") from None
                delay = retry_delay(None, attempt)
            finally:
                if part:
                    part.unlink(missing_ok=True)
                await self.window.release()
            if delay is not None:
                await asyncio.sleep(delay)

    async def playlist(self, url, depth=0):
        if depth > 5:
            raise AccessError("The playlist contains too many nested variants.")
        text, final_url = await self.transfer(url)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            raise AccessError("This URL did not return an M3U8 playlist. Use the actual stream URL from the source page.")
        variables = {}
        for line in lines:
            if line.startswith("#EXT-X-DEFINE:"):
                values = attributes(line)
                if "NAME" in values and "VALUE" in values:
                    variables[values["NAME"]] = values["VALUE"]
                else:
                    raise CompatibilityError("This playlist uses imported HLS variables; trying FFmpeg compatibility mode.")
        for _ in range(6):
            substituted = [re.sub(r"\{\$([^}]+)\}", lambda match: variables.get(match[1], match[0]), line) for line in lines]
            if substituted == lines:
                break
            lines = substituted
        variants = []
        renditions = []
        for index, line in enumerate(lines):
            attrs = attributes(line)
            if line.startswith("#EXT-X-KEY:") and ("SAMPLE-AES" in attrs.get("METHOD", "") or attrs.get("KEYFORMAT", "identity") != "identity"):
                raise AccessError("This stream uses unsupported DRM. Use an accessible, non-DRM stream.")
            for uri in re.findall(r'URI="([^"]+)"', line):
                checked_url(urljoin(final_url, uri))
            if not line.startswith("#"):
                checked_url(urljoin(final_url, line))
            if line.startswith("#EXT-X-STREAM-INF:"):
                uri = next((candidate for candidate in lines[index + 1:] if not candidate.startswith("#")), None)
                if uri:
                    resolution = attrs.get("RESOLUTION", "0x0").split("x")
                    area = int(resolution[0]) * int(resolution[1])
                    variants.append((area, int(attrs.get("BANDWIDTH", 0)), urljoin(final_url, uri), attrs))
            if line.startswith("#EXT-X-MEDIA:"):
                renditions.append(attrs)
        if variants:
            _, _, variant_url, attrs = max(variants, key=lambda v: v[:2])
            video_path = await self.playlist(variant_url, depth + 1)
            audio = [r for r in renditions if r.get("TYPE") == "AUDIO" and r.get("GROUP-ID") == attrs.get("AUDIO") and r.get("URI")]
            if not audio:
                return video_path
            selected = max(audio, key=lambda r: (r.get("DEFAULT") == "YES", r.get("AUTOSELECT") == "YES"))
            audio_path = await self.playlist(urljoin(final_url, selected["URI"]), depth + 1)
            master = self.directory / f"master-{depth}.m3u8"
            master.write_text('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",DEFAULT=YES,AUTOSELECT=YES,URI="' + audio_path.name + '"\n#EXT-X-STREAM-INF:BANDWIDTH=10000000,AUDIO="audio"\n' + video_path.name + '\n')
            return master
        if "#EXT-X-ENDLIST" not in lines:
            raise AccessError("This is a live stream. This version downloads completed, on-demand playlists.")
        media_path = self.directory / f"media-{self.media_index}.m3u8"
        self.media_index += 1
        rewritten = []
        pending_range = None
        last_uri, last_end = None, None
        duration = 0
        for line in lines:
            if line.startswith("#EXT-X-BYTERANGE:"):
                match = re.fullmatch(r"(\d+)(?:@(\d+))?", line.split(":", 1)[1])
                if not match or int(match[1]) <= 0:
                    raise AccessError("The playlist contains an invalid segment byte range.")
                pending_range = (int(match[1]), int(match[2]) if match[2] else None)
                continue
            if line.startswith("#EXTINF:"):
                seconds = float(line.split(":", 1)[1].split(",", 1)[0])
                if not math.isfinite(seconds) or seconds <= 0:
                    raise AccessError("The playlist contains an invalid video duration.")
                duration += seconds
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-MAP:")):
                attrs = attributes(line)
                if "URI" in attrs:
                    byte_range = None
                    if "BYTERANGE" in attrs:
                        parts = attrs["BYTERANGE"].split("@")
                        if len(parts) != 2:
                            raise CompatibilityError("This initialization range needs FFmpeg compatibility mode.")
                        byte_range = int(parts[1]), int(parts[0])
                        if min(byte_range) < 0 or byte_range[1] == 0:
                            raise AccessError("The playlist contains an invalid initialization byte range.")
                    name = self.asset(urljoin(final_url, attrs["URI"]), "key" if line.startswith("#EXT-X-KEY:") else "init", byte_range)
                    line = re.sub(r'URI="[^"]*"', f'URI="{name}"', line)
                    line = re.sub(r',?BYTERANGE="[^"]*"', "", line)
            elif "URI=" in line or "{$" in line:
                raise CompatibilityError("This HLS playlist feature needs FFmpeg compatibility mode.")
            elif not line.startswith("#"):
                uri = urljoin(final_url, line)
                byte_range = None
                if pending_range:
                    length, start = pending_range
                    if start is None:
                        if last_uri != uri or last_end is None:
                            raise AccessError("An implicit segment byte range has no matching previous segment.")
                        start = last_end
                    byte_range = start, length
                    last_end = start + length
                else:
                    last_end = None
                last_uri = uri
                pending_range = None
                line = self.asset(uri, byte_range=byte_range)
            rewritten.append(line)
        if pending_range or duration <= 0:
            raise AccessError("The playlist has no complete video segments.")
        self.duration = max(self.duration, duration)
        media_path.write_text("\n".join(rewritten) + "\n")
        return media_path

    async def download(self, url):
        self.directory.mkdir(parents=True, exist_ok=True)
        limits = httpx.Limits(max_connections=self.options.connections, max_keepalive_connections=self.options.connections)
        timeout = httpx.Timeout(30, connect=15, pool=30)
        async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True) as client:
            self.client = client
            local_playlist = await self.playlist(url)
            if not self.assets:
                raise AccessError("The playlist has no complete video segments.")
            self.window.maximum = min(self.options.connections, len(self.assets))
            self.window.limit = min(self.window.limit, self.window.maximum)
            self.started = time.monotonic()
            self.report(duration=self.duration, engine_used="parallel")
            queue = asyncio.Queue()
            for asset in self.assets:
                queue.put_nowait(asset)

            async def worker():
                while True:
                    try:
                        asset = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await self.transfer(asset.url, asset.path, asset.byte_range, asset.is_key)
                    self.completed += 1
                    self.progress()

            workers = [asyncio.create_task(worker()) for _ in range(self.window.maximum)]
            try:
                await asyncio.gather(*workers)
                self.progress(force=True)
                return local_playlist
            finally:
                for task in workers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
