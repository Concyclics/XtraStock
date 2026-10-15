"""Request-scoped GPU ownership for the two fixed FinAgent vLLM units."""
import asyncio
from dataclasses import dataclass
import logging
import time

import httpx

log = logging.getLogger('finagent.models')


class ModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    unit: str
    base_url: str
    served_name: str


class SystemdBackend:
    def __init__(self, spec, client):
        self.spec, self.client = spec, client

    async def command(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            '/usr/bin/systemctl', '--user', *arguments, self.spec.unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=110)
        except BaseException:
            # This is only our systemctl client, never an unrelated GPU process.
            if process.returncode is None:
                process.terminate()
            await process.wait()
            raise
        if process.returncode:
            raise ModelUnavailable(f'{self.spec.unit}: systemctl exited {process.returncode}; see journal')
        return output.decode().strip()

    async def start(self):
        await self.command('start', '--no-block')

    async def stop(self):
        await self.command('stop')

    async def failed(self):
        state = await self.command('show', '--property=ActiveState', '--value')
        return state in {'failed', 'inactive'}

    async def healthy(self):
        try:
            response = await self.client.get(self.spec.base_url + '/v1/models', timeout=1)
            return response.status_code == 200 and any(
                item.get('id') == self.spec.served_name
                for item in response.json().get('data', []))
        except (httpx.RequestError, ValueError, AttributeError, TypeError):
            return False


class ModelLease:
    def __init__(self, slot):
        self.slot = slot
        self.released = False

    async def release(self):
        async with self.slot.lock:
            if not self.released:
                self.released = True
                self.slot.requests -= 1
                self.slot.last_used = time.monotonic()


class ModelSlot:
    def __init__(self, spec, backend, start_timeout, poll_seconds):
        self.spec, self.backend = spec, backend
        self.start_timeout, self.poll_seconds = start_timeout, poll_seconds
        self.lock = asyncio.Lock()
        self.requests = 0
        self.last_used = time.monotonic()
        self.phase = 'stopped'
        self.start_task = None
        self.retry_at = 0.0
        self.last_start_seconds = None
        self.last_error = None

    async def ensure_ready(self):
        started = time.monotonic()
        self.phase = 'starting'
        cold_start = False
        try:
            if not await self.backend.healthy():
                cold_start = True
                log.info('Starting %s on demand', self.spec.unit)
                await self.backend.start()
                deadline = started + self.start_timeout
                next_state_check = started + 2
                while not await self.backend.healthy():
                    now = time.monotonic()
                    if now >= deadline:
                        raise ModelUnavailable(f'{self.spec.unit}: model startup timed out')
                    if now >= next_state_check:
                        if await self.backend.failed():
                            raise ModelUnavailable(f'{self.spec.unit}: model startup failed; see journal')
                        next_state_check = now + 5
                    await asyncio.sleep(self.poll_seconds)
            self.phase = 'ready'
            if cold_start:
                self.last_start_seconds = round(time.monotonic() - started, 3)
            self.last_error = None
        except BaseException as error:
            self.phase = 'failed'
            self.last_error = type(error).__name__
            self.retry_at = time.monotonic() + 30
            # A failed startup must not strand a half-loaded engine on the GPU.
            try:
                await self.backend.stop()
            except Exception:
                log.exception('Could not stop failed model %s', self.spec.unit)
            raise

    async def acquire(self):
        async with self.lock:
            if time.monotonic() < self.retry_at:
                raise ModelUnavailable(f'{self.spec.unit}: startup retry cooldown; see journal')
            self.requests += 1  # Includes requests waiting for a shared cold start.
            self.last_used = time.monotonic()
            lease = ModelLease(self)
            if self.start_task is None or self.start_task.done():
                # Also detects a backend that exited since the preceding request.
                self.start_task = asyncio.create_task(self.ensure_ready())
                self.start_task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception())
            start_task = self.start_task
        try:
            # One disconnected caller must not cancel startup for other callers.
            await asyncio.shield(start_task)
        except BaseException:
            await asyncio.shield(lease.release())
            raise
        return lease

    async def stop_if_idle(self, idle_seconds):
        async with self.lock:
            if self.requests or time.monotonic() - self.last_used < idle_seconds:
                return
            if self.start_task is not None and not self.start_task.done():
                return
            if self.phase == 'stopped' or time.monotonic() < self.retry_at:
                return
            self.phase = 'stopping'
            try:
                await self.backend.stop()
            except Exception as error:
                self.phase = 'stop_failed'
                self.last_error = type(error).__name__
                self.retry_at = time.monotonic() + 30
                log.exception('Idle stop failed for %s', self.spec.unit)
                return
            self.phase = 'stopped'
            self.start_task = None
            self.last_error = None
            log.info('Stopped idle %s; GPU memory released', self.spec.unit)


class ModelManager:
    def __init__(self, specs, client, idle_seconds=120, start_timeout=300,
                 poll_seconds=1, sweep_seconds=2, backend_factory=SystemdBackend):
        if idle_seconds <= 0 or start_timeout <= 0:
            raise ValueError('Model idle/start timeouts must be positive')
        self.idle_seconds = idle_seconds
        self.sweep_seconds = sweep_seconds
        self.slots = {spec.name: ModelSlot(spec, backend_factory(spec, client),
                                          start_timeout, poll_seconds) for spec in specs}
        self.sweeper = None

    def start(self):
        self.sweeper = asyncio.create_task(self.sweep())

    async def sweep(self):
        while True:
            await asyncio.sleep(self.sweep_seconds)
            for slot in self.slots.values():
                await slot.stop_if_idle(self.idle_seconds)

    async def acquire(self, name):
        return await self.slots[name].acquire()

    def status(self):
        return {'mode': 'on_demand', 'idle_timeout_seconds': self.idle_seconds,
                'models': {name: {'state': slot.phase, 'active_requests': slot.requests,
                                  'last_start_seconds': slot.last_start_seconds,
                                  'last_error': slot.last_error}
                           for name, slot in self.slots.items()}}

    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for slot in self.slots.values():
            if slot.start_task and not slot.start_task.done():
                slot.start_task.cancel()
                await asyncio.gather(slot.start_task, return_exceptions=True)
        # Stop only these two dedicated FinAgent units, including a backend
        # started manually or left behind by a preceding gateway process.
        results = await asyncio.gather(*(slot.backend.stop() for slot in self.slots.values()),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
