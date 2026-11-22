"""Real streamed ASGI bodies through the shared bound used by B4 and B5."""

import asyncio

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from app.arm2.bulk_parse import MAX_BYTES
from app.arm2.bulk_routes import BoundedRoute as BulkRoute
from app.arm2.http_limits import bounded_route
from app.arm2.questionnaire_parse import MAX_COMPRESSED
from app.arm2.questionnaire_routes import BoundedRoute as QuestionnaireRoute


def streamed(route, parts, *, expected=200):
    app = FastAPI()
    router = APIRouter(route_class=route)
    called = []

    @router.post("/")
    async def echo(request: Request):
        body = await request.body()
        called.append(True)
        return {"size": len(body), "again": len(await request.body())}

    app.include_router(router)

    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def chunks():
                for part in parts:
                    yield part
            # A deliberately false small Content-Length must not bypass streaming checks.
            return await client.post("/", content=chunks(), headers={"Content-Length": "1"})

    response = asyncio.run(send())
    assert response.status_code == expected
    assert bool(called) == (expected == 200)
    return response


@pytest.mark.parametrize("route,limit,message", [
    (BulkRoute, MAX_BYTES, "Запрос больше 5 МиБ"),
    (QuestionnaireRoute, MAX_COMPRESSED, "Транспорт больше 32 МиБ"),
])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_real_consumer_limits(route, limit, message, delta):
    size = limit + delta
    parts = [b"x" * (size // 2), b"", b"x" * (size - size // 2)]
    response = streamed(route, parts, expected=413 if delta > 0 else 200)
    assert response.json() == ({"detail": message} if delta > 0 else {"size": size, "again": size})


def test_empty_and_many_tiny_chunks_and_stop_before_reading_excess_tail():
    route = bounded_route(1024, "bounded")
    assert streamed(route, [b"", b""]).json() == {"size": 0, "again": 0}
    assert streamed(route, [b"x", b""] * 1024).json() == {"size": 1024, "again": 1024}

    def chunks():
        yield b"x" * 1024
        yield b"x"
        raise AssertionError("The rejected request must not consume another chunk")
    assert streamed(route, chunks(), expected=413).json() == {"detail": "bounded"}


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_are_configuration_errors(limit):
    with pytest.raises(ValueError):
        bounded_route(limit, "invalid")
