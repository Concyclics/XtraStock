"""Route model requests through authenticated SSH forwards to available nodes."""
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import time
from urllib.parse import urlparse

import httpx

from model_lifecycle import ModelUnavailable


@dataclass(frozen=True)
class Node:
    name: str
    base_url: str
    token: str


class RemoteLease:
    def __init__(self, pool, node, model):
        self.pool, self.node, self.model, self.released = pool, node, model, False

    async def release(self):
        if not self.released:
            self.released = True
            key = (self.node.name, self.model)
            self.pool.inflight[key] = max(0, self.pool.inflight.get(key, 0) - 1)


class RemotePool:
    def __init__(self, nodes, client, status_ttl=2):
        if not nodes or len({n.name for n in nodes}) != len(nodes):
            raise ValueError('Configure distinct inference nodes')
        self.nodes, self.client, self.status_ttl = nodes, client, status_ttl
        self.cache, self.cache_until = {}, 0.0
        self.lock = asyncio.Lock()
        self.inflight, self.cooldown = {}, {}

    @classmethod
    def from_file(cls, path, client):
        config = json.loads(Path(path).read_text())
        nodes = []
        for row in config['nodes']:
            url = row['base_url'].rstrip('/')
            parsed = urlparse(url)
            if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
                raise ValueError('Remote nodes must use loopback SSH forwards')
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Credentials belong in a private token file')
            token = Path(row['token_file']).expanduser().read_text().strip()
            if len(token) < 32:
                raise ValueError('Node token is missing or too short')
            nodes.append(Node(row['name'], url, token))
        return cls(nodes, client, config.get('status_ttl_seconds', 2))

    async def snapshots(self, force=False):
        if not force and time.monotonic() < self.cache_until:
            return self.cache
        async with self.lock:
            if not force and time.monotonic() < self.cache_until:
                return self.cache

            async def inspect(node):
                try:
                    response = await self.client.get(node.base_url + '/internal/models/status',
                        headers={'Authorization': 'Bearer ' + node.token}, timeout=5)
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict) or not isinstance(data.get('models'), dict):
                        raise ValueError('Invalid node status')
                    return node.name, data
                except (httpx.HTTPError, ValueError):
                    return node.name, {'unavailable': True, 'models': {}}

            self.cache = dict(await asyncio.gather(*(inspect(node) for node in self.nodes)))
            self.cache_until = time.monotonic() + self.status_ttl
            return self.cache

    def candidates(self, model, snapshots):
        choices = []
        for node in self.nodes:
            status = snapshots.get(node.name, {}).get('models', {}).get(model, {})
            if time.monotonic() < self.cooldown.get((node.name, model), 0):
                continue
            state = status.get('state')
            warm = state in {'ready', 'starting'}
            if not warm and not status.get('can_start'):
                continue
            # Reuse warm weights. With multiple warm replicas choose fewer requests.
            load = max(status.get('active_requests', 0), self.inflight.get((node.name, model), 0))
            headroom = max((g.get('available_mib', 0)
                            for g in snapshots.get(node.name, {}).get('gpus', [])), default=0)
            choices.append(((0 if warm else 1, load, -headroom, node.name), node))
        return [node for _, node in sorted(choices, key=lambda item: item[0])]

    async def forward(self, model, endpoint, payload, *, warmup=False):
        snapshots = await self.snapshots()
        candidates = self.candidates(model, snapshots)
        if not candidates:
            # A previously busy GPU may have become available since the cached poll.
            candidates = self.candidates(model, await self.snapshots(force=True))
        for node in candidates:
            key = (node.name, model)
            self.inflight[key] = self.inflight.get(key, 0) + 1
            lease = RemoteLease(self, node, model)
            path = '/internal/models/warmup' if warmup else '/v1/' + endpoint
            try:
                response = await self.client.send(self.client.build_request(
                    'POST', node.base_url + path, json=payload,
                    headers={'Authorization': 'Bearer ' + node.token}), stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                await lease.release()
                self.cooldown[key] = time.monotonic() + 5
                self.cache_until = 0
                continue
            except BaseException:
                await lease.release()
                raise
            if response.status_code in {502, 503}:
                # These are explicit node startup/capacity failures, before a
                # successful body. Do not replay an interrupted response stream.
                await response.aclose()
                await lease.release()
                self.cooldown[key] = time.monotonic() + 5
                self.cache_until = 0
                continue
            response.headers['x-finagent-node'] = node.name
            return response, lease
        raise ModelUnavailable('Remote GPU capacity is temporarily unavailable; retry later')

    async def warmup(self, model):
        response, lease = await self.forward(model, '', {'model': model}, warmup=True)
        try:
            await response.aread()
            response.raise_for_status()
        finally:
            await response.aclose()
            await lease.release()
        return await self.status()

    async def status(self):
        return {'mode': 'remote_on_demand', 'local_gpu_fallback': False,
                'nodes': await self.snapshots(force=True)}
