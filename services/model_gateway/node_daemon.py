"""Authenticated, loopback-only CPU daemon supervising two on-demand GPU models."""
from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import os
from pathlib import Path
import time

import anyio
from fastapi import FastAPI, HTTPException, Request
import httpx

from gpu_allocator import GPUAllocator, ProcessBackend
from inference_stream import LeasedStreamingResponse, response_headers
from model_lifecycle import ModelManager, ModelSpec, ModelUnavailable


def create_app(config=None, token=None):
    if config is None:
        config = json.loads(Path(os.environ['FINAGENT_NODE_CONFIG']).read_text())
    token = token or Path(config['token_file']).read_text().strip()
    if len(token) < 32:
        raise ValueError('Node authentication requires a private random token')
    models = {model['model']: model for model in config['models']}
    aliases = {model['served_name']: name for name, model in models.items()}
    aliases.update({name: name for name in models})
    allocator = GPUAllocator(config.get('gpu_guard_mib', 1024),
                             config.get('max_external_utilization', 15), config.get('allowed_gpus'))
    specs = [ModelSpec(name, 'node-' + model['name'],
                       f"http://127.0.0.1:{model['port']}", model['served_name'])
             for name, model in models.items()]

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10),
                                     limits=httpx.Limits(max_connections=64)) as client:
            manager = ModelManager(
                specs, client, idle_seconds=config.get('idle_seconds', 120),
                start_timeout=config.get('start_timeout_seconds', 420),
                backend_factory=lambda spec, c: ProcessBackend(
                    spec, c, allocator, models[spec.name], config, token))
            app.state.models, app.state.client = manager, client
            manager.start()
            try:
                yield
            finally:
                await manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.allocator = allocator

    def authenticate(request):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            raise HTTPException(401, 'Authentication required')

    @app.get('/health')
    async def health():
        return {'status': 'running', 'mode': 'gpu_node_on_demand'}

    @app.get('/internal/models/status')
    async def status(request: Request):
        authenticate(request)
        manager = app.state.models
        result = manager.status()
        result.update(node=config['node_name'])
        try:
            inventory = await allocator.inventory({n: m['memory_mib'] for n, m in models.items()})
            result['gpus'] = inventory['gpus']
        except (ModelUnavailable, ValueError, OSError, asyncio.TimeoutError):
            inventory = {'capacity': {name: False for name in models}}
            result.update(gpus=[], inventory_error='GPU inventory unavailable')
        for name, slot in manager.slots.items():
            result['models'][name].update(
                can_start=inventory['capacity'][name] and time.monotonic() >= slot.retry_at,
                gpu_uuid=slot.backend.gpu_uuid, reserved_mib=models[name]['memory_mib'])
        return result

    async def body(request):
        authenticate(request)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, 'Invalid JSON')
        if (not isinstance(payload, dict) or not isinstance(payload.get('model'), str)
                or payload['model'] not in aliases):
            raise HTTPException(400, 'Unsupported model')
        return payload, aliases[payload['model']]

    @app.post('/internal/models/warmup')
    async def warmup(request: Request):
        _, name = await body(request)
        try:
            lease = await app.state.models.acquire(name)
        except ModelUnavailable:
            raise HTTPException(503, 'No ready GPU capacity on this node', headers={'Retry-After': '30'})
        with anyio.CancelScope(shield=True):
            await lease.release()
        return app.state.models.status()

    @app.post('/v1/{endpoint:path}')
    async def inference(endpoint: str, request: Request):
        payload, name = await body(request)
        model = models[name]
        if endpoint not in model['endpoints']:
            raise HTTPException(400, 'Endpoint does not match the configured model')
        payload['model'] = model['served_name']
        lease = None
        try:
            lease = await app.state.models.acquire(name)
            url = lease.slot.spec.base_url + '/v1/' + endpoint
            response = await app.state.client.send(app.state.client.build_request(
                'POST', url, json=payload, headers={'Authorization': 'Bearer ' + token}), stream=True)
        except ModelUnavailable:
            raise HTTPException(503, 'No ready GPU capacity on this node', headers={'Retry-After': '30'})
        except BaseException as error:
            if lease:
                with anyio.CancelScope(shield=True):
                    await lease.release()
            if isinstance(error, httpx.RequestError):
                raise HTTPException(502, 'Model backend unavailable') from None
            raise
        headers = response_headers(response)
        headers['x-finagent-node'] = config['node_name']
        return LeasedStreamingResponse(response, lease, headers)

    return app
