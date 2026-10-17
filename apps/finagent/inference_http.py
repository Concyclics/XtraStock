"""Wait for explicit GPU capacity responses without spending document retries."""
import logging
import time

import requests


class InferenceCancelled(requests.Timeout):
    """The owner requested a normal stop while waiting for GPU capacity."""


def post_with_capacity_wait(session, url, *, timeout=480, deadline=None,
                            stop_requested=None, **kwargs):
    waiting = False
    while True:
        if stop_requested and stop_requested():
            raise InferenceCancelled('Inference interrupted at a capacity-wait boundary')
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise requests.Timeout('Inference capacity wait reached its task deadline')
        response = session.post(url, timeout=timeout if remaining is None else min(timeout, remaining), **kwargs)
        if (getattr(response, 'status_code', None) != 503
                or response.headers.get('x-finagent-retryable') != 'capacity'):
            return response
        try:
            delay = max(1, min(120, float(response.headers.get('Retry-After', 30))))
        except (TypeError, ValueError):
            delay = 30
        response.close()
        if not waiting:
            logging.info('Waiting for inference GPU capacity; document retry count is unchanged')
            waiting = True
        wake_at = time.monotonic() + delay
        if deadline is not None:
            wake_at = min(wake_at, deadline)
        while time.monotonic() < wake_at:
            if stop_requested and stop_requested():
                raise InferenceCancelled('Inference interrupted at a capacity-wait boundary')
            time.sleep(min(1, max(0, wake_at - time.monotonic())))
