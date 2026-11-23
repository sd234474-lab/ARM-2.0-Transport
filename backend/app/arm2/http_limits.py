"""One ARM2 transport bound; limits and operator messages belong to consumers."""

from fastapi import HTTPException
from fastapi.routing import APIRoute


def bounded_route(maximum: int, detail: str) -> type[APIRoute]:
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("A positive byte limit is required")

    class BoundedBodyRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def bounded(request):
                if request.method not in {"GET", "HEAD"}:
                    body = bytearray()
                    async for chunk in request.stream():
                        # Check before retaining any bytes of an excessive chunk.
                        # One buffer also bounds overhead for many tiny/empty chunks.
                        if len(body) + len(chunk) > maximum:
                            raise HTTPException(413, detail)
                        body.extend(chunk)
                    request._body = bytes(body)
                return await handler(request)

            return bounded

    return BoundedBodyRoute
