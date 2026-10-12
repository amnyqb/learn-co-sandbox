"""Observe request traffic across jobs, retries, redirects, and relay fetching."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

import httpx

from downloader import AccessError, DEFAULT_CONNECTIONS, HLSDownloader, retry_delay
from relay import FFmpegRelay


def options(url='https://source.example/video.m3u8', connections=DEFAULT_CONNECTIONS):
    return SimpleNamespace(url=url, connections=connections, retries=1,
        user_agent='fixture', referer='', headers={}, cookies='')


class DelayedBody(httpx.AsyncByteStream):
    def __init__(self, transport):
        self.transport = transport
        self.closed = False

    async def __aiter__(self):
        await asyncio.sleep(.35)
        yield b'segment'

    async def aclose(self):
        if not self.closed:
            self.closed = True
            self.transport.active -= 1


class ObservedTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.starts = []

    async def handle_async_request(self, request):
        self.starts.append(time.monotonic())
        self.active += 1
        self.peak = max(self.peak, self.active)
        return httpx.Response(200, stream=DelayedBody(self))


class SourceLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_jobs_share_ceiling_and_pacing(self):
        observed = ObservedTransport()
        with tempfile.TemporaryDirectory() as directory:
            jobs = [HLSDownloader(options(), directory) for _ in range(2)]
            async with AsyncExitStack() as stack:
                for job in jobs:
                    job.client = await stack.enter_async_context(httpx.AsyncClient(transport=observed))
                    await stack.enter_async_context(job.source_session())
                await asyncio.gather(*(job.transfer(f'https://source.example/{i}.ts', Path(directory)/f'{j}-{i}.ts')
                    for j, job in enumerate(jobs) for i in range(8)))
                self.assertIs(await jobs[0].source_window(options().url), await jobs[1].source_window(options().url))
            self.assertGreaterEqual(observed.peak, 2)
            self.assertLessEqual(observed.peak, 4)
            self.assertEqual(observed.active, 0)
            self.assertEqual(len(observed.starts), 16)
            self.assertTrue(all(b-a >= .095 for a, b in zip(observed.starts, observed.starts[1:])))
            self.assertTrue(all(path.read_bytes() == b'segment' for path in Path(directory).glob('*.ts')))
            self.assertFalse(any(job.sources for job in jobs))

    async def test_throttle_pauses_other_job_and_keeps_limit_after_completion(self):
        for status in (429, 503):
            with self.subTest(status=status):
                starts = []
                # Distinct origins prevent the first subtest's cooldown affecting the second.
                url = f'https://source-{status}.example/segment.ts'
                def handler(request):
                    starts.append(time.monotonic())
                    return httpx.Response(status, headers={'Retry-After': '0.1'}) if len(starts) == 1 else httpx.Response(200, content=b'segment')
                jobs = [HLSDownloader(options(url), '.') for _ in range(2)]
                async with AsyncExitStack() as stack:
                    for job in jobs:
                        job.client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                        await stack.enter_async_context(job.source_session())
                    await asyncio.gather(*(job.transfer(url) for job in jobs))
                    window = await jobs[0].source_window(url)
                    self.assertLessEqual(window.limit, 2)
                    self.assertGreaterEqual(starts[1]-starts[0], .95)
                self.assertFalse(window.owners)
                # A fresh job can't reset source throttling immediately.
                new = HLSDownloader(options(url), '.')
                async with new.source_session():
                    self.assertIs(await new.source_window(url), window)
                    self.assertLessEqual(window.limit, 2)

    async def test_long_retry_after_stops_without_retrying_or_resetting_cooldown(self):
        self.assertEqual(retry_delay('600', 0), 600)
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(429, headers={'Retry-After': '600'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(2):
                job = HLSDownloader(options(), '.')
                job.client = client
                async with job.source_session():
                    with self.assertRaisesRegex(AccessError, 'cooldown longer than five minutes'):
                        await job.transfer(options().url)
            self.assertEqual(len(requests), 1)

    async def test_redirects_and_relay_use_same_origin_budget_and_cancel_releases_slot(self):
        starts = []
        def handler(request):
            starts.append(str(request.url))
            if request.url.path == '/redirect':
                return httpx.Response(302, headers={'Location': 'https://cdn.example/segment.ts'})
            return httpx.Response(200, content=b'segment')
        with tempfile.TemporaryDirectory() as directory:
            job = HLSDownloader(options(), directory)
            relay = FFmpegRelay(options(), directory)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client, job.source_session(), relay.transport.source_session():
                job.client = relay.transport.client = client
                await job.transfer('https://source.example/redirect', Path(directory)/'redirect.ts')
                # Registration normally uses the running relay's loopback address.
                relay.base = 'http://127.0.0.1:1'
                name = relay.register('https://cdn.example/other.ts').rsplit('/', 1)[-1]
                await relay.materialize(relay.resources[name])
                self.assertIs(await job.source_window('https://cdn.example/segment.ts'), await relay.transport.source_window('https://cdn.example/other.ts'))
                self.assertIsNot(await job.source_window(options().url), await job.source_window('https://cdn.example/segment.ts'))
                window = await job.source_window(options().url)
                await window.throttle(2)
                task = asyncio.create_task(job.transfer(options().url))
                await asyncio.sleep(.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(window.active, 0)
                self.assertEqual(job.window.active, 0)
            self.assertEqual(len(starts), 3)
