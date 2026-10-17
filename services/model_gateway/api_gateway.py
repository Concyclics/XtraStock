"""Private loopback gateway for the migrated local OCR/vector services."""

from xtrastock.config import bootstrap_environment
bootstrap_environment()

from contextlib import asynccontextmanager
import hmac
import logging
import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import anyio
import httpx

from model_lifecycle import ModelManager, ModelSpec, ModelUnavailable
from remote_pool import RemotePool

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
# HTTP client logging includes request URLs; our own lifecycle log records only units.
logging.getLogger('httpx').setLevel(logging.WARNING)

from runtime_paths import RUNTIME_STATE
ROOT = RUNTIME_STATE
from xtrastock.config import Settings
settings = Settings()
config = {**dotenv_values(ROOT / '.env.gateway'), **settings.legacy_environment()}
config.setdefault('FINAGENT_CLOUD_BASE_URL', settings.get('api.base_url'))
config.setdefault('FINAGENT_CLOUD_KEY', settings.get('api.key', ''))
secret = config.get('FINAGENT_GATEWAY_KEY', '')
if not secret:
    raise RuntimeError('Configure FINAGENT_GATEWAY_KEY before starting the gateway')
OCR_MODEL = 'deepseek-ai/DeepSeek-OCR-2'
EMBED_MODEL = 'Qwen/Qwen3-Embedding-0.6B'
CLOUD_MODEL = 'gpt-6-luna'
MODEL_SPECS = [
    ModelSpec(OCR_MODEL, 'finagent-ocr-model.service', settings.get('gateway.ocr_backend_url'), OCR_MODEL),
    ModelSpec(EMBED_MODEL, 'finagent-embedding-model.service', settings.get('gateway.embedding_backend_url'),
              'Qwen3-Embedding-0.6B')]

@asynccontextmanager
async def lifespan(app):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=20),
                                         limits=httpx.Limits(max_connections=40))
    pool_config = os.environ.get('FINAGENT_REMOTE_POOL_CONFIG', '')
    app.state.remote_pool = RemotePool.from_file(pool_config, app.state.client) if pool_config else None
    app.state.models = None
    if app.state.remote_pool is None:
        app.state.models = ModelManager(
            MODEL_SPECS, app.state.client,
            idle_seconds=float(os.environ.get('FINAGENT_MODEL_IDLE_SECONDS', '120')),
            start_timeout=float(os.environ.get('FINAGENT_MODEL_START_TIMEOUT_SECONDS', '300')))
        app.state.models.start()
    try:
        yield
    finally:
        try:
            if app.state.models is not None:
                await app.state.models.close()
        finally:
            await app.state.client.aclose()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def authenticated(request):
    provided = request.headers.get('authorization', '')
    if not hmac.compare_digest(provided, 'Bearer ' + secret):
        raise HTTPException(401, 'Authentication required')

@app.get('/health')
async def health():
    return {'status': 'running',
            'mode': 'remote_on_demand' if getattr(app.state, 'remote_pool', None) else 'on_demand',
            'scope': 'CPU gateway; models start only for inference or explicit warmup'}

@app.get('/v1/models')
async def models(request: Request):
    authenticated(request)
    return {'object': 'list', 'data': [
        {'id': m, 'object': 'model', 'owned_by': owner}
        for m, owner in [(OCR_MODEL, 'local'), (EMBED_MODEL, 'local'), (CLOUD_MODEL, 'cloud')]]}

@app.get('/internal/models/status')
async def model_status(request: Request):
    authenticated(request)
    if getattr(request.app.state, 'remote_pool', None):
        return await request.app.state.remote_pool.status()
    return request.app.state.models.status()

@app.post('/internal/models/warmup')
async def warmup(request: Request):
    authenticated(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, 'Invalid JSON')
    if not isinstance(payload, dict) or payload.get('model') not in {OCR_MODEL, EMBED_MODEL}:
        raise HTTPException(400, 'Choose one of the two local models')
    try:
        if getattr(request.app.state, 'remote_pool', None):
            return await request.app.state.remote_pool.warmup(payload['model'])
        lease = await request.app.state.models.acquire(payload['model'])
    except ModelUnavailable as error:
        raise HTTPException(503, str(error), headers={'Retry-After': '30', 'x-finagent-retryable': 'capacity'})
    with anyio.CancelScope(shield=True):
        await lease.release()
    return request.app.state.models.status()


class LeasedStreamingResponse(StreamingResponse):
    """Keep GPU ownership through streaming, send failures and disconnects."""
    def __init__(self, response, lease, headers):
        super().__init__(response.aiter_raw(), status_code=response.status_code, headers=headers)
        self.upstream_response, self.lease = response, lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette's disconnect task group can cancel the body iterator;
            # cleanup still has to close the upstream and release the GPU lease.
            with anyio.CancelScope(shield=True):
                try:
                    await self.upstream_response.aclose()
                finally:
                    if self.lease:
                        await self.lease.release()

@app.post('/v1/{endpoint:path}')
async def proxy(endpoint: str, request: Request):
    authenticated(request)
    if endpoint not in {'embeddings', 'responses', 'chat/completions', 'completions'}:
        raise HTTPException(404, 'Unsupported endpoint')
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, 'Invalid JSON')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'JSON must be an object')
    model = payload.get('model')
    headers = {'content-type': 'application/json'}
    local_model = None
    if endpoint == 'embeddings':
        if model not in {EMBED_MODEL, 'Qwen3-Embedding-0.6B'}:
            raise HTTPException(400, 'Embedding model differs from migrated vectors')
        payload['model'] = 'Qwen3-Embedding-0.6B'
        upstream = 'http://127.0.0.1:8084/v1/'
        local_model = EMBED_MODEL
    elif model == OCR_MODEL:
        if endpoint not in {'chat/completions', 'completions'}:
            raise HTTPException(400, 'OCR uses the chat/completions API')
        upstream = 'http://127.0.0.1:8083/v1/'
        local_model = OCR_MODEL
        payload.setdefault('skip_special_tokens', False)
        payload.setdefault('vllm_xargs', {'ngram_size': 30, 'window_size': 90,
                                         'whitelist_token_ids': [128821, 128822]})
    elif model == CLOUD_MODEL:
        upstream = config['FINAGENT_CLOUD_BASE_URL'].rstrip('/') + '/'
        headers['authorization'] = 'Bearer ' + config['FINAGENT_CLOUD_KEY']
    else:
        raise HTTPException(400, 'Unsupported model')
    client = request.app.state.client
    lease = None
    try:
        pool = getattr(request.app.state, 'remote_pool', None)
        if local_model and pool:
            response, lease = await pool.forward(local_model, endpoint, payload)
        else:
            if local_model:
                lease = await request.app.state.models.acquire(local_model)
            response = await client.send(client.build_request('POST', upstream + endpoint,
                                                              json=payload, headers=headers), stream=True)
    except ModelUnavailable as error:
        raise HTTPException(503, str(error), headers={'Retry-After': '30', 'x-finagent-retryable': 'capacity'})
    except httpx.RequestError:
        if lease:
            with anyio.CancelScope(shield=True):
                await lease.release()
        # Do not log request payloads, headers, credentials, or document images.
        raise HTTPException(502, 'Upstream unavailable; see individual service status')
    except BaseException:
        if lease:
            with anyio.CancelScope(shield=True):
                await lease.release()
        raise
    forwarded_headers = {k: v for k, v in response.headers.items()
                         if k.lower() in {'content-type', 'content-encoding', 'x-request-id', 'retry-after',
                                          'x-finagent-node'}}
    return LeasedStreamingResponse(response, lease, forwarded_headers)
