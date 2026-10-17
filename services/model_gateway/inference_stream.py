"""Keep inference leases until the response body is consumed or disconnected."""
import anyio
from fastapi.responses import StreamingResponse


class LeasedStreamingResponse(StreamingResponse):
    def __init__(self, response, lease=None, headers=None):
        super().__init__(response.aiter_raw(), status_code=response.status_code, headers=headers)
        self.upstream_response, self.lease = response, lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.upstream_response.aclose()
                finally:
                    if self.lease:
                        await self.lease.release()


def response_headers(response):
    return {k: v for k, v in response.headers.items()
            if k.lower() in {'content-type', 'content-encoding', 'x-request-id', 'retry-after'}}
