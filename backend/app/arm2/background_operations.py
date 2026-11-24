"""ARM2 worker. PostgreSQL owns the queue and a lost worker releases its lease."""
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.arm2.operation_models import ACTIVE, BackgroundOperation
from app.config import base_settings
from app.db import engine
from app.organization_context import organization_scope
from app.organization_registry import scopes

log = logging.getLogger(__name__)


def work_once(database, handlers):
    """At most one operation per workspace across server processes."""
    with database.connect().execution_options(isolation_level="AUTOCOMMIT") as lease:
        key = "arm2-background-operations"
        if not lease.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 6365))"), {"key": key}):
            return False
        try:
            with Session(database, expire_on_commit=False) as db:
                job = db.scalar(select(BackgroundOperation)
                    .where(BackgroundOperation.state.in_(ACTIVE), BackgroundOperation.kind.in_(handlers),
                           or_(BackgroundOperation.not_before.is_(None), BackgroundOperation.not_before <= func.now()))
                    .order_by(BackgroundOperation.created_at, BackgroundOperation.id).limit(1).with_for_update())
                if job is None:
                    return False
                job.state = "RUNNING"
                job.progress = {"phase": "PREPARING"}
                job.updated_at = func.now()
                db.commit()

            def report(phase, done=0, total=0):
                with Session(database) as db:
                    db.execute(update(BackgroundOperation).where(BackgroundOperation.id == job.id).values(
                        progress={"phase": phase, "done": done, "total": total}, updated_at=func.now()))
                    db.commit()

            failure = handlers[job.kind](database, job, report)
            delay = failure.pop("retry_after", None) if failure else None
            with Session(database) as db:
                db.execute(update(BackgroundOperation).where(BackgroundOperation.id == job.id).values(
                    state="QUEUED" if delay else "FAILED" if failure else "SUCCEEDED",
                    failure=failure, updated_at=func.now(),
                    not_before=datetime.now(UTC) + timedelta(seconds=delay) if delay else None))
                db.commit()
            return True
        finally:
            lease.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 6365))"), {"key": key})


def operation_lifespan(handlers):
    @asynccontextmanager
    async def lifespan(_app):
        stop = Event()

        def run():
            while not stop.is_set():
                worked = False
                try:
                    for scope in scopes() if base_settings().organizations_enabled else [None]:
                        if stop.is_set():
                            break
                        try:
                            with organization_scope(scope):
                                worked = work_once(engine(), handlers) or worked
                        except Exception as error:
                            log.warning("arm2_operation_worker_error=%s", type(error).__name__)
                except Exception as error:
                    log.warning("arm2_operation_scope_error=%s", type(error).__name__)
                stop.wait(0.2 if worked else 2)

        thread = Thread(target=run, name="arm2-operations", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            await run_in_threadpool(thread.join, 2)

    return lifespan
