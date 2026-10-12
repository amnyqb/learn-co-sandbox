"""Regression coverage for low macOS file limits and storage error diagnosis."""

import asyncio
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from downloader import AccessError, HLSDownloader, resource


class SegmentHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith('.m3u8'):
            data = ('#EXTM3U\n#EXT-X-TARGETDURATION:1\n' + ''.join(
                f'#EXTINF:1,\n{i}.ts\n' for i in range(96)) + '#EXT-X-ENDLIST\n').encode()
        else:
            data = bytes([int(self.path.rsplit('/', 1)[-1].split('.')[0])]) * 131072
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data[:65536])
            if len(data) > 65536:
                # Keep each socket and partial segment file open together.
                time.sleep(.15)
                self.wfile.write(data[65536:])
        except (BrokenPipeError, ConnectionResetError):
            pass


class SegmentServer(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


class StorageRegressionTests(unittest.TestCase):
    @unittest.skipIf(resource is None, 'Requires POSIX file limits')
    def test_two_jobs_under_256_file_limit(self):
        server = SegmentServer(('127.0.0.1', 0), SegmentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        script = '''
import asyncio, resource, sys
from pathlib import Path
from types import SimpleNamespace
from downloader import HLSDownloader, configure_file_limit
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
configure_file_limit()
soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
assert soft >= (2048 if hard == resource.RLIM_INFINITY else min(2048, hard))
# Simulate a machine that cannot raise its hard limit; don't change the parent.
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
configure_file_limit()
async def run():
    options = SimpleNamespace(url=sys.argv[1], connections=128, retries=0,
        user_agent='fixture', referer='', headers={}, cookies='')
    jobs = [HLSDownloader(options, Path(sys.argv[2])/str(i)) for i in range(2)]
    assert all(job.connection_limit == 32 for job in jobs)
    playlists = await asyncio.gather(*(job.download(options.url) for job in jobs))
    assert options.connections == 128
    for playlist in playlists:
        names = [line for line in playlist.read_text().splitlines() if not line.startswith('#')]
        assert len(names) == 96
        for i, name in enumerate(names):
            assert (playlist.parent/name).read_bytes() == bytes([i])*131072
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 256
asyncio.run(run())
'''
        try:
            with tempfile.TemporaryDirectory(prefix='vid-down-fd-test-') as directory:
                result = subprocess.run([sys.executable, '-c', script,
                    f'http://127.0.0.1:{server.server_port}/video.m3u8', directory],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_storage_failures_are_distinguished_and_cleanup_releases_window(self):
        async def run():
            with tempfile.TemporaryDirectory(prefix='vid-down-storage-test-') as directory:
                options = SimpleNamespace(url='https://source.example/video.m3u8',
                    connections=16, retries=0, user_agent='fixture', referer='', headers={}, cookies='')
                downloader = HLSDownloader(options, directory)
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, content=b'segment'))) as client:
                    downloader.client = client
                    for code, expected in ((errno.EMFILE, 'open-file limit'), (errno.ENOSPC, 'disk is full'),
                                           (errno.EACCES, 'not writable'), (errno.EROFS, 'read-only')):
                        with self.subTest(errno=code):
                            with patch.object(Path, 'open', side_effect=OSError(code, 'secret-signed-url')):
                                with self.assertRaises(AccessError) as error:
                                    await downloader.transfer('https://source.example/segment.ts', Path(directory)/'segment.ts')
                            self.assertIn(expected, str(error.exception))
                            self.assertNotIn('secret-signed-url', str(error.exception))
                            self.assertEqual(downloader.window.active, 0)
                            self.assertFalse(list(Path(directory).glob('*.part')))
        asyncio.run(run())

    def test_wrapped_socket_file_limit_reports_resource_exhaustion(self):
        async def run():
            def failed(request):
                raise httpx.ConnectError('secret-signed-url') from OSError(errno.EMFILE, 'too many files')
            options = SimpleNamespace(url='https://source.example/video.m3u8',
                connections=16, retries=4, user_agent='fixture', referer='', headers={}, cookies='')
            downloader = HLSDownloader(options, '.')
            async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
                downloader.client = client
                with self.assertRaises(AccessError) as error:
                    await downloader.transfer(options.url)
                self.assertIn('open-file limit', str(error.exception))
                self.assertNotIn('secret-signed-url', str(error.exception))
                self.assertEqual(downloader.window.active, 0)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
