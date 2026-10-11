"""A job-scoped loopback relay for native FFmpeg HLS compatibility.

All upstream traffic uses the same verified, credential-scoped HTTPX transport.
FFmpeg only sees opaque loopback URLs, never cookies or signed source URLs.
"""

import asyncio
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
import uuid

import httpx

from downloader import AccessError, HLSDownloader, checked_url


class FFmpegRelay:
    def __init__(self, options, directory):
        self.options = options
        self.directory = Path(directory)
        self.transport = HLSDownloader(options, directory)
        self.resources = {}
        self.identities = {}
        self.connections = set()
        self.server = None
        self.error = None

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15))
        self.transport.client = self.client
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=16384)
        self.base = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        await self.client.aclose()

    def register(self, url, kind="segment"):
        checked_url(url)
        key = url, kind
        if key not in self.identities:
            extension = Path(urlsplit(url).path).suffix.lower()
            extension = ".m3u8" if kind == "playlist" else ".key" if kind == "key" else extension if extension in {".ts", ".m4s", ".mp4", ".aac"} else ".ts"
            name = uuid.uuid4().hex + extension
            self.identities[key] = name
            self.resources[name] = {"url": url, "kind": kind, "lock": asyncio.Lock(), "path": self.directory / name}
        return self.base + "/" + self.identities[key]

    def rewrite(self, text, base):
        lines = text.splitlines()
        result = []
        next_playlist = False
        for line in lines:
            line = line.strip()
            if "{$" in line:
                raise AccessError("This stream uses unresolved HLS variables. Use a direct media playlist URL.")
            if line.startswith("#"):
                is_media = line.startswith("#EXT-X-MEDIA:")
                is_key = line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"))
                def replace(match):
                    url = urljoin(base, match[1])
                    kind = "key" if is_key else "playlist" if is_media or urlsplit(url).path.lower().endswith(".m3u8") else "segment"
                    return 'URI="' + self.register(url, kind) + '"'
                line = re.sub(r'URI="([^"]+)"', replace, line)
                if re.search(r'URI=(?!")', line):
                    raise AccessError("The playlist contains a malformed URI attribute. Use a valid media playlist URL.")
                if line.startswith("#EXT-X-STREAM-INF:"):
                    next_playlist = True
            elif line:
                url = urljoin(base, line)
                line = self.register(url, "playlist" if next_playlist else "segment")
                next_playlist = False
            result.append(line)
        return "\n".join(result) + "\n"

    async def materialize(self, resource):
        async with resource["lock"]:
            path = resource["path"]
            if path.exists():
                return path
            if resource["kind"] == "playlist":
                text, final_url = await self.transport.transfer(resource["url"])
                if not text.lstrip('\ufeff').startswith("#EXTM3U"):
                    raise AccessError("The source did not return an M3U8 playlist.")
                # A master is allowed; every media playlist must be finite.
                if "#EXT-X-STREAM-INF:" not in text and "#EXT-X-ENDLIST" not in text:
                    raise AccessError("This is a live stream. Use a completed on-demand playlist.")
                if "SAMPLE-AES" in text or re.search(r'KEYFORMAT="(?!identity")[^"]+"', text):
                    raise AccessError("This stream uses unsupported DRM. Use an accessible, non-DRM stream.")
                path.write_text(self.rewrite(text, final_url))
            else:
                await self.transport.transfer(resource["url"], path=path, is_key=resource["kind"] == "key")
            return path

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            lines = request.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            resource = self.resources.get(target.lstrip("/"))
            if method not in {"GET", "HEAD"} or resource is None:
                writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            path = await self.materialize(resource)
            size = path.stat().st_size
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            range_value = next((value.strip() for name, value in headers.items() if name.lower() == "range"), "")
            start, end = 0, size - 1
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_value)
            if match:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            if start > end or start >= size:
                writer.write(f"HTTP/1.0 416 Range Not Satisfiable\r\nContent-Range: bytes */{size}\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                return
            status = "206 Partial Content" if match else "200 OK"
            response_headers = f"HTTP/1.0 {status}\r\nContent-Length: {end - start + 1}\r\nConnection: close\r\nAccept-Ranges: bytes\r\n"
            if match:
                response_headers += f"Content-Range: bytes {start}-{end}/{size}\r\n"
            writer.write((response_headers + "\r\n").encode())
            await writer.drain()
            if method == "GET":
                with path.open("rb") as handle:
                    handle.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = handle.read(min(65536, remaining))
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
                        remaining -= len(chunk)
        except AccessError as error:
            self.error = str(error)
            writer.write(b"HTTP/1.0 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            self.error = self.error or "FFmpeg compatibility transport could not read this resource. Check the source and available disk space."
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            self.connections.discard(task)
