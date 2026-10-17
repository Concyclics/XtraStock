"""Remote scheduling regressions; no GPU, SSH, database, or cloud calls."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from gpu_allocator import GPU, GPUAllocator, ProcessBackend
from model_lifecycle import ModelUnavailable
from node_daemon import create_app
from remote_pool import Node, RemotePool

MODEL = 'test-embedding'


def node_state(state='stopped', capacity=True, requests=0, memory=40000):
    return {'models': {MODEL: {'state': state, 'can_start': capacity,
                              'active_requests': requests}},
            'gpus': [{'available_mib': memory}]}


class AllocationTests(unittest.TestCase):
    def test_foreign_compute_blocks_even_a_low_utilization_gpu_with_free_memory(self):
        allocator = GPUAllocator()
        gpu = GPU(0, 'a', 49152, 800, 0, (123,))
        with patch.object(allocator, 'owns_process', return_value=False):
            self.assertEqual(allocator.candidates([gpu], 16000), [])
            self.assertEqual(allocator.available(gpu)[1], 'other_compute_process')

    def test_busy_or_unconfigured_gpu_is_never_selected(self):
        allocator = GPUAllocator(allowed_gpus=['1'])
        self.assertEqual(allocator.candidates([
            GPU(0, 'a', 49152, 0, 0), GPU(1, 'b', 49152, 100, 94)], 16000), [])

    def test_pending_reservations_prevent_double_allocation(self):
        allocator = GPUAllocator()
        gpu = GPU(0, 'a', 24576, 0, 0)
        allocator.reservations['ocr'] = {'gpu_uuid': 'a', 'memory_mib': 16384, 'pgid': None}
        self.assertEqual(allocator.candidates([gpu], 8192), [])

    def test_own_models_can_share_large_gpu_without_counting_memory_twice(self):
        allocator = GPUAllocator()
        gpu = GPU(0, 'a', 49152, 14500, 85, (123,))
        allocator.reservations['ocr'] = {'gpu_uuid': 'a', 'memory_mib': 16384, 'pgid': 123}
        with patch.object(allocator, 'owns_process', return_value=True):
            self.assertEqual(allocator.available(gpu), (31744, 'available'))
            self.assertEqual(len(allocator.candidates([gpu], 8192)), 1)

    def test_memory_guard_and_actual_usage_are_respected(self):
        allocator = GPUAllocator(guard_mib=1024)
        self.assertEqual(allocator.candidates([GPU(0, 'a', 24576, 8000, 0)], 16384), [])


class RemotePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.states = {'a': node_state(), 'b': node_state()}
        self.codes, self.posts, self.gets = {}, [], []
        self.nodes = [Node(name, 'http://' + name, 'test-private-token-' + name)
                      for name in ['a', 'b']]

        async def upstream(request):
            host = request.url.host
            self.assertEqual(request.headers['authorization'], 'Bearer test-private-token-' + host)
            if request.method == 'GET':
                self.gets.append(host)
                return httpx.Response(200, json=self.states[host])
            self.posts.append(host)
            code = self.codes.get(host, 200)
            if code == 'disconnect':
                raise httpx.ConnectError('synthetic transport failure')
            if code == 'wait':
                await asyncio.sleep(60)
            return httpx.Response(code, json={'data': [{'embedding': [0.1, 0.2]}]})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.pool = RemotePool(self.nodes, self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def infer(self):
        response, lease = await self.pool.forward(MODEL, 'embeddings', {'model': MODEL, 'input': 'test'})
        await response.aread()
        await response.aclose()
        await lease.release()
        await lease.release()
        return response

    async def test_busy_node_is_avoided_and_node_key_never_returns_to_caller(self):
        self.states['a'] = node_state(capacity=False)
        response = await self.infer()
        self.assertEqual(self.posts, ['b'])
        self.assertEqual(response.headers['x-finagent-node'], 'b')
        self.assertNotIn('test-private-token', str(response.headers))
        self.assertEqual(self.pool.inflight[('b', MODEL)], 0)

    async def test_warm_replica_preferred_to_cold_gpu_and_status_never_starts_models(self):
        self.states['b'] = node_state(state='ready', capacity=False, requests=8)
        await self.pool.status()
        self.assertEqual(self.posts, [])
        await self.infer()
        self.assertEqual(self.posts, ['b'])

    async def test_multiple_warm_replicas_choose_lower_load(self):
        self.states['a'] = node_state(state='ready', requests=8)
        self.states['b'] = node_state(state='ready', requests=1)
        await self.infer()
        self.assertEqual(self.posts, ['b'])

    async def test_raced_capacity_retries_other_node_once(self):
        self.codes['a'] = 503
        await self.infer()
        self.assertEqual(self.posts, ['a', 'b'])
        self.assertEqual(self.pool.inflight[('a', MODEL)], 0)

    async def test_broken_tunnel_uses_other_available_node(self):
        self.codes['a'] = 'disconnect'
        await self.infer()
        self.assertEqual(self.posts, ['a', 'b'])

    async def test_all_busy_returns_unavailable_without_starting_any_model(self):
        self.states = {name: node_state(capacity=False) for name in ['a', 'b']}
        with self.assertRaises(ModelUnavailable):
            await self.infer()
        self.assertEqual(self.posts, [])

    async def test_cancelled_request_releases_scheduler_load(self):
        self.codes['a'] = 'wait'
        task = asyncio.create_task(self.infer())
        for _ in range(100):
            if self.posts:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.pool.inflight[('a', MODEL)], 0)

    async def test_nonretryable_client_error_does_not_duplicate_inference(self):
        self.codes['a'] = 400
        response = await self.infer()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.posts, ['a'])


class NodeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_unauthenticated_invalid_and_wrong_endpoint_never_start_gpu(self):
        config = {'node_name': 'test-node', 'models': [
            {'name': 'embedding', 'model': MODEL, 'served_name': MODEL, 'port': 18888,
             'memory_mib': 8192, 'endpoints': ['embeddings']}]}
        token = 'synthetic-node-authentication-token-12345'
        app = create_app(config, token)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node') as client:
            with patch.object(ProcessBackend, 'start') as start:
                self.assertEqual((await client.get('/health')).status_code, 200)
                self.assertEqual((await client.get('/internal/models/status')).status_code, 401)
                self.assertEqual((await client.post('/v1/embeddings', json={'model': MODEL})).status_code, 401)
                for payload in [[], {'model': []}, {'model': 'unknown'}]:
                    response = await client.post('/v1/embeddings', json=payload,
                                                headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 400)
                response = await client.post('/v1/chat/completions', json={'model': MODEL},
                                            headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(response.status_code, 400)
                start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
