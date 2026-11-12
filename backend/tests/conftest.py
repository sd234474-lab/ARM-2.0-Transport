import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine


@pytest.fixture(autouse=True)
def test_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("APP_ORIGIN", "http://testserver")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "test-secret-" * 4)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture(scope="session")
def database_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL/PostGIS integration tests")
    if make_url(url).database == "contractor_max_test":
        raise RuntimeError("Protected ARM1 prototype; use an isolated disposable database")
    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("Use a disposable database whose name ends with _test")
    from alembic.config import Config

    from alembic import command

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    settings.cache_clear()
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    config = Config(str(__import__("pathlib").Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    admin_engine.dispose()
    yield url
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    settings.cache_clear()


@pytest.fixture
def isolated_arm2_database_url():
    from pathlib import Path
    from re import fullmatch
    from uuid import uuid4

    from alembic.config import Config

    from alembic import command

    source = os.getenv("TEST_DATABASE_URL")
    if not source:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL/PostGIS integration tests")
    source_url = make_url(source)
    if source_url.database == "contractor_max_test":
        raise RuntimeError("Protected ARM1 prototype; use an isolated disposable database")
    if not source_url.database or not source_url.database.endswith("_test"):
        raise RuntimeError("Use a disposable database whose name ends with _test")

    database_name = f"arm2_{uuid4().hex}_test"
    isolated_url = source_url.set(database=database_name)
    admin_url = source_url.set(database="postgres")
    database_created = False
    try:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as connection:
                exists = connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": database_name},
                )
                if exists is not None:
                    raise RuntimeError(
                        f"Disposable database already exists: {database_name}"
                    )
                connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
                database_created = True
        finally:
            admin_engine.dispose()

        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = isolated_url.render_as_string(hide_password=False)
        settings.cache_clear()
        try:
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            command.upgrade(config, "head")
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
            settings.cache_clear()
        yield isolated_url.render_as_string(hide_password=False)
    finally:
        if database_created:
            if (
                database_name == source_url.database
                or fullmatch(r"arm2_[0-9a-f]{32}_test", database_name) is None
            ):
                raise RuntimeError(
                    f"Refusing to drop unexpected database: {database_name}"
                )
            cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
            try:
                with cleanup_engine.connect() as connection:
                    exists = connection.scalar(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": database_name},
                    )
                    if exists is not None:
                        connection.exec_driver_sql(
                            f'DROP DATABASE "{database_name}" WITH (FORCE)'
                        )
            finally:
                cleanup_engine.dispose()


@pytest.fixture
def db(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    settings.cache_clear()
    if engine.cache_info().currsize:
        engine().dispose()
    engine.cache_clear()
    from app.models import Base

    with engine().begin() as conn:
        tables = ", ".join('"' + table.name + '"' for table in reversed(Base.metadata.sorted_tables))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with Session(engine(), expire_on_commit=False) as session:
        from app.catalog import WORK_TYPE_DISPLAY_ORDER, WORK_TYPES
        from app.models import DiskFolderSettings, ExchangeState, WorkType
        session.add(DiskFolderSettings(id=1, version=1, folder_sets=[]))
        session.add_all([WorkType(code=code, name=names[0], filename_label=names[1],
                                 model_index=i, sort_order=WORK_TYPE_DISPLAY_ORDER.index(code)+1)
                         for i, (code, names) in enumerate(WORK_TYPES.items())])
        session.add(ExchangeState(id=1, instance_id="11111111-1111-4111-8111-111111111111",
                                  source_timezone="Asia/Yekaterinburg", intake_cohort="test"))
        session.commit()
        yield session
    engine().dispose()
    engine.cache_clear()


@pytest.fixture
def client(db):
    from app.api import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def users(db):
    from app.auth import hasher
    from app.models import User

    for login, role in [("admin", "ADMIN_OPERATOR"), ("operator", "OPERATOR")]:
        db.add(
            User(
                login=login,
                name=login,
                role=role,
                active=True,
                password_hash=hasher.hash("test-password-123"),
            )
        )
    db.commit()


@pytest.fixture
def admin(client, users):
    response = client.post("/api/auth/login", json={"login": "admin", "password": "test-password-123"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["csrf"]
    return client
