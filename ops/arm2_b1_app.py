"""Exact own-sandbox ARM2 route boundary, no extra integration processes."""
from arm2_b1_check import LOCAL, VISUAL_DB, allowed, config, guard

guard()

from app.api import app as original  # noqa: E402
from app.db import engine  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402


def health():
    from pathlib import Path

    with engine().connect() as connection:
        assert connection.scalar(text('SELECT current_database()')) == VISUAL_DB
        assert Path(connection.scalar(text('SHOW data_directory'))).resolve() == (LOCAL / 'pgdata').resolve()
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0061'
    return {'ok': True, 'nonce': config()['nonce'], 'sandbox': 'B1'}


async def app(scope, receive, send):
    if scope['type'] == 'http':
        if not allowed(scope['method'], scope['path']):
            return await JSONResponse({'detail': 'Outside B1 scope'}, status_code=404)(scope, receive, send)
        if scope['path'] == '/api/health':
            return await JSONResponse(await run_in_threadpool(health))(scope, receive, send)
    return await original(scope, receive, send)
