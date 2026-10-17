"""Allocate only idle GPUs or GPUs already owned by this inference daemon.

No global process termination, scheduler changes, or GPU resets are performed.
The lock serializes our reservations; external schedulers can still race us, in
which case model startup fails and the request can use another node.
"""
import asyncio
import csv
from dataclasses import dataclass
import io
import os
from pathlib import Path
import signal

import httpx

from model_lifecycle import ModelUnavailable


@dataclass(frozen=True)
class GPU:
    index: int
    uuid: str
    total_mib: int
    used_mib: int
    utilization: int
    processes: tuple[int, ...] = ()


async def command_output(*args):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ModelUnavailable('GPU inventory unavailable')
    return output.decode()


async def gpu_snapshot():
    devices = await command_output(
        'nvidia-smi', '--query-gpu=index,uuid,memory.total,memory.used,utilization.gpu',
        '--format=csv,noheader,nounits')
    processes = await command_output(
        'nvidia-smi', '--query-compute-apps=gpu_uuid,pid', '--format=csv,noheader,nounits')
    by_gpu = {}
    for row in csv.reader(io.StringIO(processes)):
        if row:
            by_gpu.setdefault(row[0].strip(), []).append(int(row[1].strip()))
    result = []
    for row in csv.reader(io.StringIO(devices)):
        if not row:
            continue
        index, uuid, total, used, utilization = [value.strip() for value in row]
        result.append(GPU(int(index), uuid, int(total), int(used), int(utilization),
                          tuple(by_gpu.get(uuid, []))))
    return result


class GPUAllocator:
    def __init__(self, guard_mib=1024, max_external_utilization=15, allowed_gpus=None,
                 snapshot=gpu_snapshot):
        self.guard_mib = guard_mib
        self.max_external_utilization = max_external_utilization
        self.allowed_gpus = set(map(str, allowed_gpus or []))
        self.snapshot = snapshot
        self.lock = asyncio.Lock()
        self.reservations = {}

    def owns_process(self, pid):
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            # A process can disappear between the two nvidia-smi queries.
            return True
        except OSError:
            return False
        return any(r.get('pgid') == group for r in self.reservations.values())

    def available(self, gpu):
        if self.allowed_gpus and not ({str(gpu.index), gpu.uuid} & self.allowed_gpus):
            return 0, 'not_configured'
        if any(not self.owns_process(pid) for pid in gpu.processes):
            return 0, 'other_compute_process'
        committed = sum(r['memory_mib'] for r in self.reservations.values()
                        if r['gpu_uuid'] == gpu.uuid)
        if not committed and gpu.utilization > self.max_external_utilization:
            return 0, 'busy'
        return max(0, gpu.total_mib - max(gpu.used_mib, committed) - self.guard_mib), 'available'

    def candidates(self, gpus, memory_mib):
        choices = [(gpu, self.available(gpu)[0]) for gpu in gpus]
        return sorted((item for item in choices if item[1] >= memory_mib),
                      key=lambda item: (-item[1], item[0].index))

    async def inventory(self, requirements):
        async with self.lock:
            gpus = await self.snapshot()
            return {
                'gpus': [dict(index=g.index, uuid=g.uuid, total_mib=g.total_mib,
                              used_mib=g.used_mib, utilization=g.utilization,
                              available_mib=self.available(g)[0],
                              availability=self.available(g)[1]) for g in gpus],
                'capacity': {name: bool(self.candidates(gpus, memory))
                             for name, memory in requirements.items()},
            }


class ProcessBackend:
    """Fixed, operator-configured commands; request bodies never become argv."""
    def __init__(self, spec, client, allocator, model, config, token):
        self.spec, self.client, self.allocator = spec, client, allocator
        self.model, self.config, self.token = model, config, token
        self.process = None
        self.gpu_uuid = None

    async def start(self):
        async with self.allocator.lock:
            if self.process is not None and self.process.returncode is None:
                return
            choices = self.allocator.candidates(await self.allocator.snapshot(), self.model['memory_mib'])
            if not choices:
                raise ModelUnavailable('No idle GPU has enough unreserved memory')
            gpu, _ = choices[0]
            # Leave runtime headroom inside our reservation as well as the GPU guard.
            fraction = (self.model['memory_mib'] - 2048) / gpu.total_mib
            if not 0 < fraction < 1:
                raise ModelUnavailable('Invalid configured GPU memory budget')
            self.allocator.reservations[self.spec.name] = {
                'gpu_uuid': gpu.uuid, 'memory_mib': self.model['memory_mib'], 'pgid': None}
            env = os.environ.copy()
            # vLLM 0.21's NVML platform adapter requires numeric device IDs.
            # Resolve the index on every start; keep UUIDs for reservations/status.
            env.update(CUDA_VISIBLE_DEVICES=str(gpu.index), CUDA_DEVICE_ORDER='PCI_BUS_ID',
                       VLLM_API_KEY=self.token,
                       VLLM_USE_FLASHINFER_SAMPLER='0', VLLM_NO_USAGE_STATS='1',
                       HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
            args = [self.config['vllm_bin'], 'serve', self.model['path'],
                    '--served-model-name', self.spec.served_name,
                    '--host', '127.0.0.1', '--port', str(self.model['port']),
                    '--gpu-memory-utilization', f'{fraction:.6f}', *self.model['arguments']]
            log_dir = Path(self.config['log_dir'])
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_file = (log_dir / (self.model['name'] + '.log')).open('ab')
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *args, env=env, cwd=self.config['runtime_dir'],
                    stdout=log_file, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                self.gpu_uuid = gpu.uuid
                self.allocator.reservations[self.spec.name]['pgid'] = self.process.pid
            except BaseException:
                self.allocator.reservations.pop(self.spec.name, None)
                raise
            finally:
                log_file.close()

    async def stop(self):
        # ModelSlot holds its own request lock across this operation. Allocator
        # reservations stay in place until every child in our process group exits.
        process = self.process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.config.get('stop_timeout_seconds', 60))
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            # vLLM can exit its API process before an engine child has stopped.
            for _ in range(50):
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        async with self.allocator.lock:
            self.allocator.reservations.pop(self.spec.name, None)
            self.process = None
            self.gpu_uuid = None

    async def failed(self):
        return self.process is None or self.process.returncode is not None

    async def healthy(self):
        if await self.failed():
            return False
        try:
            response = await self.client.get(
                self.spec.base_url + '/v1/models',
                headers={'Authorization': 'Bearer ' + self.token}, timeout=2)
            return response.status_code == 200 and any(
                item.get('id') == self.spec.served_name for item in response.json().get('data', []))
        except (httpx.RequestError, ValueError, AttributeError, TypeError):
            return False
