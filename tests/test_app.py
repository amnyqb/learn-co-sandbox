"""Real FFmpeg/HLS integration tests; no external video services required."""

import functools
import asyncio
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient


class QuietHandler(SimpleHTTPRequestHandler):
    counts = {}
    lock = threading.Lock()
    active = 0
    peak = 0
    seen_headers = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.lock:
            self.counts[self.path] = self.counts.get(self.path, 0) + 1
            count = self.counts[self.path]
        if self.path.startswith('/redirect-auth/'):
            self.send_response(302)
            self.send_header('Location', f'http://localhost:{self.server.server_port}/capture/video.m3u8')
            self.end_headers()
            return
        if self.path.startswith('/capture/'):
            with self.lock:
                self.seen_headers.append(dict(self.headers))
            self.path = self.path.removeprefix('/capture')
        if self.path.startswith('/auth/'):
            if (self.headers.get('Cookie') != 'session=test' or self.headers.get('X-Test-Key') != 'fixture-key'
                    or self.headers.get('User-Agent') != 'test-browser' or self.headers.get('Referer') != 'https://source.example/watch'):
                self.send_response(403)
                self.end_headers()
                return
            self.path = self.path.removeprefix('/auth')
        if self.path.startswith('/rate/'):
            if not self.path.endswith('.m3u8') and count == 1:
                self.send_response(429)
                self.send_header('Retry-After', '0.01')
                self.end_headers()
                return
            self.path = self.path.removeprefix('/rate')
        if self.path.startswith('/bench/'):
            if not self.path.endswith('.m3u8'):
                with self.lock:
                    type(self).active += 1
                    type(self).peak = max(type(self).peak, type(self).active)
                try:
                    time.sleep(.08)
                    index = int(self.path.rsplit('/', 1)[-1].split('.')[0])
                    data = bytes([index]) * 8192
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with self.lock:
                        type(self).active -= 1
                return
            self.path = self.path.removeprefix('/bench')
        if self.path.startswith('/slow/'):
            if not self.path.endswith('.m3u8'):
                time.sleep(2)
            self.path = self.path.removeprefix('/slow')
        if self.path == '/range.ts' and self.headers.get('Range'):
            data = Path(self.translate_path(self.path)).read_bytes()
            start, end = (int(part) for part in self.headers['Range'].removeprefix('bytes=').split('-'))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
            self.send_header('Content-Length', str(end - start + 1))
            self.end_headers()
            self.wfile.write(data[start:end + 1])
            return
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when a running FFmpeg process is cancelled.


class FixtureServer(ThreadingHTTPServer):
    request_queue_size = 256

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(10)
        return connection, address

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ssl.SSLError):
            super().handle_error(request, client_address)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class AppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='vid-down-tests-')
        cls.root = Path(cls.temporary.name)
        cls.media = cls.root / 'media'
        cls.media.mkdir()
        os.environ['VID_DOWN_DATA'] = str(cls.root / 'data')
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '4', '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-crf', '18',
            '-c:a', 'aac', '-b:a', '128k', '-g', '24', '-f', 'hls', '-hls_time', '1',
            '-hls_playlist_type', 'vod', '-hls_segment_filename', str(cls.media / 'segment-%03d.ts'),
            str(cls.media / 'video.m3u8'),
        ], check=True)
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', str(cls.media / 'video.m3u8'), '-vf', 'scale=320:180', '-an',
            '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-g', '24',
            '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
            '-hls_segment_filename', str(cls.media / 'low-%03d.ts'), str(cls.media / 'low.m3u8'),
        ], check=True)
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', str(cls.media / 'video.m3u8'), '-vn', '-c:a', 'aac',
            '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
            '-hls_segment_filename', str(cls.media / 'audio-%03d.ts'), str(cls.media / 'audio.m3u8'),
        ], check=True)
        (cls.media / 'master.m3u8').write_text(
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=320x180,AUDIO="audio"\nlow.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,AUDIO="audio"\nvideo.m3u8\n')
        playlist = (cls.media / 'video.m3u8').read_text()
        (cls.media / 'live.m3u8').write_text(playlist.replace('#EXT-X-ENDLIST', ''))
        (cls.media / 'drm.m3u8').write_text('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n#EXT-X-ENDLIST\n')
        (cls.media / 'private.m3u8').write_text('#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n')
        cls.server = FixtureServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(cls.media)))
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        (cls.media / 'bench.m3u8').write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n' + ''.join(f'#EXTINF:1,\n{index}.ts\n' for index in range(24)) + '#EXT-X-ENDLIST\n')
        (cls.media / 'key.bin').write_bytes(os.urandom(16))
        (cls.media / 'key-info.txt').write_text(cls.url + '/key.bin\n' + str(cls.media / 'key.bin') + '\n')
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', str(cls.media / 'video.m3u8'), '-c', 'copy', '-f', 'hls', '-hls_time', '1',
            '-hls_playlist_type', 'vod', '-hls_key_info_file', str(cls.media / 'key-info.txt'),
            '-hls_segment_filename', str(cls.media / 'encrypted-%03d.ts'), str(cls.media / 'encrypted.m3u8'),
        ], check=True)
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', str(cls.media / 'video.m3u8'), '-c', 'copy', '-f', 'hls', '-hls_time', '1',
            '-hls_playlist_type', 'vod', '-hls_flags', 'single_file',
            '-hls_segment_filename', str(cls.media / 'range.ts'), str(cls.media / 'range.m3u8'),
        ], check=True)
        # Exercise implicit offsets after the first range too.
        lines = (cls.media / 'range.m3u8').read_text().splitlines()
        first = True
        for index, line in enumerate(lines):
            if line.startswith('#EXT-X-BYTERANGE:'):
                if not first:
                    lines[index] = line.split('@')[0]
                first = False
        (cls.media / 'range.m3u8').write_text('\n'.join(lines) + '\n')
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', str(cls.media / 'video.m3u8'), '-c', 'copy', '-f', 'hls', '-hls_time', '1',
            '-bsf:a', 'aac_adtstoasc', '-hls_playlist_type', 'vod', '-hls_segment_type', 'fmp4',
            '-hls_fmp4_init_filename', 'init.mp4', '-hls_segment_filename', str(cls.media / 'fragment-%03d.m4s'),
            str(cls.media / 'fragmented.m3u8'),
        ], check=True)
        import app
        cls.module = importlib.reload(app)
        cls.client = TestClient(cls.module.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()
        cls.temporary.cleanup()
        os.environ.pop('VID_DOWN_DATA', None)

    def create(self, **kwargs):
        payload = {'url': self.url + '/video.m3u8', 'name': 'test-video', **kwargs}
        response = self.client.post('/api/jobs', json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def wait(self, job):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = next(item for item in self.client.get('/api/jobs').json() if item['id'] == job['id'])
            if current['status'] in {'completed', 'failed', 'cancelled'}:
                return current
            time.sleep(.15)
        self.fail('Video processing timed out')

    def metadata(self, job):
        path = self.root / 'data' / job['id'] / job['filename']
        return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))

    def check_completed(self, job, video_codec, audio_codec):
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(job['progress'], 100)
        metadata = self.metadata(job)
        streams = metadata['streams']
        video = next(s for s in streams if s['codec_type'] == 'video')
        audio = next(s for s in streams if s['codec_type'] == 'audio')
        self.assertEqual(video['codec_name'], video_codec)
        self.assertEqual(audio['codec_name'], audio_codec)
        self.assertAlmostEqual(float(metadata['format']['duration']), 4, delta=.3)
        self.assertGreater(job['output_bytes'], 1000)
        self.assertFalse((self.root / 'data' / job['id'] / 'source.mkv').exists())
        self.assertFalse(list((self.root / 'data' / job['id']).glob('*.partial.*')))
        file_response = self.client.get(f"/api/jobs/{job['id']}/file")
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(len(file_response.content), job['output_bytes'])
        self.assertIn('attachment', file_response.headers['content-disposition'])
        preview = self.client.get(f"/api/jobs/{job['id']}/file?preview=true", headers={'Range': 'bytes=0-1023'})
        self.assertEqual(preview.status_code, 206)
        self.assertEqual(len(preview.content), 1024)
        return video

    def test_mp4_compression_and_copy(self):
        for preset in ('small', 'balanced', 'original'):
            with self.subTest(preset=preset):
                job = self.wait(self.create(preset=preset))
                self.check_completed(job, 'h264', 'aac')
                if preset != 'original':
                    self.assertLess(job['output_bytes'], job['source_bytes'])

    def test_hevc_and_webm(self):
        for format, video, audio in [('hevc', 'hevc', 'aac'), ('webm', 'vp9', 'opus')]:
            with self.subTest(format=format):
                job = self.wait(self.create(format=format, preset='small'))
                self.check_completed(job, video, audio)
                self.assertLess(job['output_bytes'], job['source_bytes'])

    def test_target_size_all_formats(self):
        for format, video, audio in [('mp4', 'h264', 'aac'), ('hevc', 'hevc', 'aac'), ('webm', 'vp9', 'opus')]:
            with self.subTest(format=format):
                job = self.wait(self.create(format=format, target_mb=.3, preset='small'))
                self.check_completed(job, video, audio)
                self.assertLessEqual(job['output_bytes'], 300_000 * 1.05)
                self.assertGreater(job['output_bytes'], 300_000 * .7)

    def test_master_selects_highest_resolution_and_keeps_audio(self):
        job = self.wait(self.create(url=self.url + '/master.m3u8', preset='original'))
        video = self.check_completed(job, 'h264', 'aac')
        self.assertEqual(video['height'], 720)

    def test_cancel_cleanup_and_remove(self):
        job = self.create(url=self.url + '/slow/video.m3u8')
        time.sleep(.3)
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}").status_code, 409)
        cancelled = self.client.post(f"/api/jobs/{job['id']}/cancel").json()
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/file").status_code, 409)
        folder = self.root / 'data' / job['id']
        self.assertEqual({p.name for p in folder.iterdir()}, {'job.json'})
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}").status_code, 200)
        self.assertFalse(folder.exists())

    def test_bad_live_drm_and_local_file_playlists(self):
        for name, message in [('missing.m3u8', 'HTTP 404'), ('live.m3u8', 'live stream'), ('drm.m3u8', 'DRM'), ('private.m3u8', 'unsupported')]:
            with self.subTest(playlist=name):
                job = self.wait(self.create(url=self.url + '/' + name))
                self.assertEqual(job['status'], 'failed')
                self.assertIn(message, job['error'])
                self.assertNotIn('/etc/passwd', job['error'])

    def test_validation_and_local_access(self):
        for payload in [{'url': 'file:///etc/passwd'}, {'url': '        '}, {'url': self.url + '/video.m3u8', 'target_mb': -1}, {'url': self.url + '/video.m3u8', 'preset': 'original', 'format': 'webm'},
                        {'url': self.url + '/video.m3u8', 'connections': 129}, {'url': self.url + '/video.m3u8', 'connections': 0},
                        {'url': self.url + '/video.m3u8', 'headers': {'X-Key': 'bad\r\nheader'}},
                        {'url': self.url + '/video.m3u8', 'headers': {'Host': 'other.example'}},
                        {'url': self.url + '/video.m3u8', 'fallback_urls': ['file:///etc/passwd']}]:
            self.assertEqual(self.client.post('/api/jobs', json=payload).status_code, 422)
        self.assertEqual(self.client.post('/api/jobs', json={'url': self.url + '/video.m3u8'}, headers={'Origin': 'https://unrelated.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/jobs/unknown/file').status_code, 404)
        self.assertTrue(self.client.get('/api/health').json()['ok'])
        home = self.client.get('/')
        self.assertEqual(home.status_code, 200)
        self.assertIn('Your stream. Your file.', home.text)

    def test_parallel_speed_order_and_bound(self):
        from downloader import HLSDownloader

        async def run(connections, name):
            downloader = HLSDownloader(self.module.DownloadRequest(url=self.url + '/bench/bench.m3u8', connections=connections), self.root / name)
            started = time.monotonic()
            path = await downloader.download(self.url + '/bench/bench.m3u8')
            elapsed = time.monotonic() - started
            lines = [line for line in path.read_text().splitlines() if not line.startswith('#')]
            for index, line in enumerate(lines):
                self.assertEqual((path.parent / line).read_bytes(), bytes([index]) * 8192)
            return elapsed

        serial = asyncio.run(run(1, 'serial'))
        with QuietHandler.lock:
            QuietHandler.peak = 0
        parallel = asyncio.run(run(16, 'parallel'))
        with QuietHandler.lock:
            peak = QuietHandler.peak
        # Paced starts avoid a burst of 16 requests to this short-latency source.
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 16)
        self.assertLess(parallel, serial / 2)
        print(f'\nTransport benchmark: serial={serial:.3f}s, parallel={parallel:.3f}s, speedup={serial / parallel:.1f}x, peak={peak} requests')

    def test_rate_limit_backoff_and_authentication(self):
        job = self.wait(self.create(url=self.url + '/rate/video.m3u8', preset='original'))
        self.check_completed(job, 'h264', 'aac')
        self.assertLess(job['connections'], job['total_segments'])
        denied = self.wait(self.create(url=self.url + '/auth/video.m3u8', retries=0))
        self.assertEqual(denied['status'], 'failed')
        self.assertIn('HTTP 403', denied['error'])
        accepted = self.wait(self.create(url=self.url + '/auth/video.m3u8', preset='original',
                                        cookies='session=test', headers={'X-Test-Key': 'fixture-key'},
                                        user_agent='test-browser', referer='https://source.example/watch'))
        self.check_completed(accepted, 'h264', 'aac')
        saved = (self.root / 'data' / accepted['id'] / 'job.json').read_text()
        for secret in ['session=test', 'fixture-key', 'source.example/watch']:
            self.assertNotIn(secret, saved)

    def test_encryption_byte_ranges_and_fmp4(self):
        for playlist in ['encrypted.m3u8', 'range.m3u8', 'fragmented.m3u8']:
            with self.subTest(playlist=playlist):
                job = self.wait(self.create(url=self.url + '/' + playlist, preset='original', engine='parallel'))
                self.check_completed(job, 'h264', 'aac')
                self.assertEqual(job['engine_used'], 'parallel')
                self.assertFalse((self.root / 'data' / job['id'] / 'segments').exists())

    def test_alternative_urls_and_ffmpeg_fallback(self):
        job = self.wait(self.create(url=self.url + '/missing.m3u8', fallback_urls=[self.url + '/video.m3u8'], preset='original'))
        self.check_completed(job, 'h264', 'aac')
        self.assertEqual(len(job['source_attempts']), 1)
        self.assertIn('HTTP 404', job['source_attempts'][0]['error'])
        direct = self.wait(self.create(engine='ffmpeg', preset='original'))
        self.check_completed(direct, 'h264', 'aac')
        self.assertEqual(direct['engine_used'], 'ffmpeg')
        authenticated = self.wait(self.create(url=self.url + '/auth/video.m3u8', engine='ffmpeg', preset='original',
                                            cookies='session=test', headers={'X-Test-Key': 'fixture-key'},
                                            user_agent='test-browser', referer='https://source.example/watch'))
        self.check_completed(authenticated, 'h264', 'aac')
        # A valid URI-bearing metadata extension needs native FFmpeg interpretation.
        playlist = (self.media / 'video.m3u8').read_text().replace('#EXTM3U', '#EXTM3U\n#EXT-X-DATERANGE:ID="meta",CLASS="custom",START-DATE="2026-01-01T00:00:00Z",URI="metadata.json"', 1)
        (self.media / 'compatibility.m3u8').write_text(playlist)
        automatic = self.wait(self.create(url=self.url + '/compatibility.m3u8', preset='original'))
        self.check_completed(automatic, 'h264', 'aac')
        self.assertEqual(automatic['engine_used'], 'ffmpeg')
        self.assertIsNotNone(automatic['fallback_reason'])

    def test_redirect_does_not_leak_credentials(self):
        for engine in ['parallel', 'ffmpeg']:
            job = self.wait(self.create(url=self.url + '/redirect-auth/video.m3u8', preset='original', engine=engine,
                                       cookies='session=test', headers={'X-Test-Key': 'fixture-key', 'Authorization': 'Bearer fixture-token'}))
            self.check_completed(job, 'h264', 'aac')
        with QuietHandler.lock:
            captured = list(QuietHandler.seen_headers)
        self.assertTrue(captured)
        for headers in captured:
            self.assertNotIn('Cookie', headers)
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('X-Test-Key', headers)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required for local TLS fixtures')
    def test_tls_verification_in_both_methods(self):
        certificate, key = self.root / 'cert.pem', self.root / 'tls-key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(certificate)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tls_server = FixtureServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(self.media)))
        tls_server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        tls_server.socket = context.wrap_socket(tls_server.socket, server_side=True, do_handshake_on_connect=False)
        thread = threading.Thread(target=tls_server.serve_forever, daemon=True)
        thread.start()
        tls_url = f'https://127.0.0.1:{tls_server.server_port}'
        lines = (self.media / 'video.m3u8').read_text().splitlines()
        (self.media / 'tls-segments.m3u8').write_text('\n'.join(line if line.startswith('#') else tls_url + '/' + line for line in lines) + '\n')
        previous_ca = os.environ.get('SSL_CERT_FILE')
        try:
            for engine in ['parallel', 'ffmpeg']:
                with self.subTest(engine=engine, trusted=False):
                    denied = self.wait(self.create(url=self.url + '/tls-segments.m3u8', engine=engine, retries=0, preset='original'))
                    self.assertEqual(denied['status'], 'failed')
                    self.assertIn('certificate verification failed', denied['error'])
                    self.assertIsNone(denied['output_bytes'])
            os.environ['SSL_CERT_FILE'] = str(certificate)
            for engine in ['parallel', 'ffmpeg']:
                with self.subTest(engine=engine, trusted=True):
                    accepted = self.wait(self.create(url=self.url + '/tls-segments.m3u8', engine=engine, retries=0, preset='original'))
                    self.check_completed(accepted, 'h264', 'aac')
        finally:
            if previous_ca is None:
                os.environ.pop('SSL_CERT_FILE', None)
            else:
                os.environ['SSL_CERT_FILE'] = previous_ca
            tls_server.shutdown()
            tls_server.server_close()
            thread.join()

    def test_restart_persists_and_marks_interrupted(self):
        job = self.wait(self.create(preset='original', name='../../unsafe.mp4'))
        self.assertEqual(job['filename'], 'unsafe.mp4')
        interrupted_id = 'a' * 32
        directory = self.root / 'data' / interrupted_id
        directory.mkdir()
        interrupted = {**job, 'id': interrupted_id, 'status': 'running'}
        (directory / 'job.json').write_text(json.dumps(interrupted))
        (directory / 'source.mkv').write_bytes(b'partial')
        restored = self.module.JobManager()
        self.assertEqual(restored.jobs[job['id']]['status'], 'completed')
        self.assertEqual(restored.jobs[interrupted_id]['status'], 'failed')
        self.assertFalse((directory / 'source.mkv').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
