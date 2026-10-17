"""Lifecycle regressions: shared startup, cancellation, streaming and idle stop."""
import asyncio
import json
import unittest

import httpx
from starlette.requests import ClientDisconnect

from unittest.mock import patch
# Lifecycle unit tests do not read the private gateway environment.
with patch("dotenv.dotenv_values", return_value={"FINAGENT_GATEWAY_KEY":"test-only", "FINAGENT_CLOUD_KEY":"test-only", "FINAGENT_CLOUD_BASE_URL":"https://cloud.invalid/v1"}):
    import api_gateway as gateway
from model_lifecycle import ModelManager, ModelSpec, ModelUnavailable


class FakeBackend:
    def __init__(self, spec, client):
        self.starts = self.stops = 0
        self.ready = False
        self.allow_start = asyncio.Event()
        self.fail_start = False
        self.become_healthy = True

    async def healthy(self):
        return self.ready

    async def start(self):
        self.starts += 1
        if self.fail_start:
            raise ModelUnavailable('test start failed')
        await self.allow_start.wait()
        self.ready = self.become_healthy

    async def stop(self):
        self.stops += 1
        self.ready = False

    async def failed(self):
        return False


class ByteStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"data":[]}'


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ModelManager(
            [ModelSpec('test', 'fixed-test.service', 'http://localhost', 'test')], None,
            idle_seconds=0.04, start_timeout=1, poll_seconds=0.005,
            sweep_seconds=0.01, backend_factory=FakeBackend)
        self.slot = self.manager.slots['test']
        self.backend = self.slot.backend

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_concurrent_start_and_busy_model_survive_idle_deadline(self):
        self.manager.start()
        callers = [asyncio.create_task(self.manager.acquire('test')) for _ in range(8)]
        await asyncio.sleep(0.02)
        self.assertEqual(self.backend.starts, 1)
        self.assertEqual(self.slot.requests, 8)
        self.backend.allow_start.set()
        leases = await asyncio.gather(*callers)
        await asyncio.sleep(0.08)
        self.assertEqual(self.backend.stops, 0)
        for lease in leases:
            await lease.release()
            await lease.release()  # Cleanup may run more than once after a disconnect.
        await asyncio.sleep(0.08)
        self.assertEqual(self.backend.stops, 1)
        self.assertEqual(self.slot.requests, 0)
        self.assertEqual(self.slot.phase, 'stopped')

    async def test_cancelled_caller_does_not_cancel_shared_startup(self):
        first = asyncio.create_task(self.manager.acquire('test'))
        second = asyncio.create_task(self.manager.acquire('test'))
        await asyncio.sleep(0.02)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.backend.allow_start.set()
        lease = await second
        self.assertEqual(self.backend.starts, 1)
        self.assertEqual(self.slot.requests, 1)
        await lease.release()

    async def test_failed_start_releases_waiters_and_does_not_retry_in_loop(self):
        self.backend.fail_start = True
        results = await asyncio.gather(*(self.manager.acquire('test') for _ in range(3)),
                                       return_exceptions=True)
        self.assertTrue(all(isinstance(result, ModelUnavailable) for result in results))
        self.assertEqual(self.slot.requests, 0)
        self.assertEqual(self.backend.starts, 1)
        self.assertEqual(self.backend.stops, 1)
        with self.assertRaises(ModelUnavailable):
            await self.manager.acquire('test')
        self.assertEqual(self.backend.starts, 1)

    async def test_backend_exit_is_detected_on_next_request(self):
        self.backend.allow_start.set()
        lease = await self.manager.acquire('test')
        await lease.release()
        self.backend.ready = False
        lease = await self.manager.acquire('test')
        self.assertEqual(self.backend.starts, 2)
        await lease.release()

    async def test_startup_timeout_stops_half_loaded_model_and_releases_request(self):
        self.backend.allow_start.set()
        self.backend.become_healthy = False
        self.slot.start_timeout = 0.025
        with self.assertRaisesRegex(ModelUnavailable, 'timed out'):
            await self.manager.acquire('test')
        self.assertEqual(self.slot.requests, 0)
        self.assertEqual(self.backend.stops, 1)

    async def test_ready_probe_does_not_overwrite_last_cold_start_duration(self):
        self.backend.allow_start.set()
        lease = await self.manager.acquire('test')
        await lease.release()
        duration = self.slot.last_start_seconds
        self.assertIsNotNone(duration)
        lease = await self.manager.acquire('test')
        self.assertEqual(self.backend.starts, 1)
        self.assertEqual(self.slot.last_start_seconds, duration)
        await lease.release()

    async def test_stream_retains_lease_and_send_failure_releases_it(self):
        self.backend.allow_start.set()
        lease = await self.manager.acquire('test')
        upstream = httpx.Response(200, stream=ByteStream())
        response = gateway.LeasedStreamingResponse(upstream, lease, {})
        scope = {'type': 'http', 'asgi': {'spec_version': '2.4'}}

        async def receive():
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] == 'http.response.body':
                await asyncio.sleep(0.06)
                await self.slot.stop_if_idle(0.04)
                self.assertEqual(self.backend.stops, 0)
                self.assertEqual(self.slot.requests, 1)
                raise OSError('client disconnected during stream')

        with self.assertRaises(ClientDisconnect):
            await response(scope, receive, send)
        self.assertEqual(self.slot.requests, 0)
        self.assertTrue(upstream.is_closed)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream_calls = 0
        self.acquisitions = 0

        async def upstream(request):
            self.upstream_calls += 1
            return httpx.Response(200, stream=ByteStream())

        async def acquire(name):
            self.acquisitions += 1
            raise ModelUnavailable('fake cold-start failure')

        class FakeManager:
            status = lambda _: {'mode': 'on_demand', 'models': {}}
        manager = FakeManager()
        manager.acquire = acquire
        gateway.app.state.models = manager
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        gateway.app.state.client = self.upstream
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                        base_url='http://gateway')
        self.auth = {'Authorization': 'Bearer ' + gateway.secret}

    async def asyncTearDown(self):
        gateway.app.state.remote_pool = None
        await self.client.aclose()
        await self.upstream.aclose()

    async def test_health_model_listing_and_unauthenticated_calls_never_start_gpu(self):
        self.assertEqual((await self.client.get('/health')).status_code, 200)
        self.assertEqual((await self.client.get('/v1/models', headers=self.auth)).status_code, 200)
        self.assertEqual((await self.client.get('/internal/models/status', headers=self.auth)).status_code, 200)
        for path in ['/v1/embeddings', '/internal/models/warmup']:
            result = await self.client.post(path, json={'model': gateway.EMBED_MODEL})
            self.assertEqual(result.status_code, 401)
        self.assertEqual(self.acquisitions, 0)

    async def test_cloud_route_does_not_start_local_models(self):
        result = await self.client.post('/v1/responses', headers=self.auth,
                                        json={'model': gateway.CLOUD_MODEL, 'input': 'synthetic test'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.acquisitions, 0)
        self.assertEqual(self.upstream_calls, 1)

    async def test_startup_failure_returns_503_and_retry_hint_without_forwarding(self):
        result = await self.client.post('/v1/embeddings', headers=self.auth,
                                        json={'model': gateway.EMBED_MODEL, 'input': 'synthetic test'})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.headers['retry-after'], '30')
        self.assertEqual(self.upstream_calls, 0)

    async def test_invalid_body_or_model_never_starts_gpu(self):
        for payload in [[], {'model': 'unknown'}]:
            result = await self.client.post('/v1/embeddings', headers=self.auth, json=payload)
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.acquisitions, 0)

    async def test_remote_route_does_not_acquire_local_gpu_or_use_cloud_client(self):
        class Pool:
            async def forward(self, model, endpoint, payload):
                assert model == gateway.EMBED_MODEL and endpoint == 'embeddings'
                return httpx.Response(200, stream=ByteStream(),
                                      headers={'x-finagent-node': 'test-node'}), None
        gateway.app.state.remote_pool = Pool()
        response = await self.client.post('/v1/embeddings', headers=self.auth,
                                         json={'model': gateway.EMBED_MODEL, 'input': 'synthetic'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['x-finagent-node'], 'test-node')
        self.assertEqual(self.acquisitions, 0)
        self.assertEqual(self.upstream_calls, 0)

    async def test_remote_capacity_failure_never_falls_back_to_local_gpu(self):
        class Pool:
            async def forward(self, *args):
                raise ModelUnavailable('synthetic no capacity')
        gateway.app.state.remote_pool = Pool()
        response = await self.client.post('/v1/embeddings', headers=self.auth,
                                         json={'model': gateway.EMBED_MODEL, 'input': 'synthetic'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['x-finagent-retryable'], 'capacity')
        self.assertEqual(self.acquisitions, 0)
        self.assertEqual(self.upstream_calls, 0)


if __name__ == '__main__':
    unittest.main()
