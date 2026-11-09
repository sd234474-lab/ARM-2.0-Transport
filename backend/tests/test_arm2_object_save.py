import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.history_models import HistoryEvent
from app.arm2.object_registry import (
    FieldSpec,
    ObjectDefinition,
    ObjectRegistry,
    ObjectRegistryError,
)
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.arm2.vehicle_store import create_vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type
from app.models import User
from app.permissions import Permission


def add_user(
    db,
    login,
    *,
    role="OPERATOR",
    permissions=None,
    active=True,
    deleted_at=None,
):
    user = User(
        login=login,
        name=login,
        password_hash="unused",
        role=role,
        active=active,
        permissions=permissions or [],
        deleted_at=deleted_at,
    )
    db.add(user)
    db.flush()
    return user


def add_admin(db, login="arm2-core-admin"):
    return add_user(db, login, role="ADMIN_OPERATOR")


def save_context(user, correlation_id="operation-001"):
    return SaveContext("HUMAN", str(user.id), "integration-test", correlation_id)


def test_shared_save_uses_current_database_permissions_and_real_history_author(
    isolated_arm2_database_url,
):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        viewer = add_user(
            db, "type-viewer", permissions=[str(Permission.ARM2_TYPE_VIEW)]
        )
        type_editor = add_user(
            db,
            "type-editor",
            permissions=[
                str(Permission.ARM2_TYPE_VIEW),
                str(Permission.ARM2_TYPE_EDIT),
            ],
        )
        vehicle_editor = add_user(
            db,
            "vehicle-editor",
            permissions=[
                str(Permission.ARM2_VEH_VIEW),
                str(Permission.ARM2_VEH_EDIT),
            ],
        )
        vehicle_viewer = add_user(
            db, "vehicle-viewer", permissions=[str(Permission.ARM2_VEH_VIEW)]
        )
        malformed = add_user(
            db, "malformed-editor", permissions=[str(Permission.ARM2_TYPE_EDIT)]
        )
        inactive = add_user(db, "inactive-admin", role="ADMIN_OPERATOR", active=False)
        deleted = add_user(
            db,
            "deleted-admin",
            role="ADMIN_OPERATOR",
            deleted_at=datetime.now(UTC),
        )
        admin = add_admin(db, "arm2-auth-admin")
        db.commit()

        denied = (
            viewer,
            vehicle_viewer,
            vehicle_editor,
            malformed,
            inactive,
            deleted,
        )
        for principal in denied:
            with pytest.raises(ObjectAuthorizationError):
                create_vehicle_type(
                    db,
                    f"Denied {principal.login}",
                    True,
                    False,
                    False,
                    principal=principal,
                    context=save_context(principal),
                )

        with pytest.raises(ObjectAuthorizationError):
            create_vehicle_type(
                db,
                "No principal",
                True,
                False,
                False,
                principal=None,
                context=SaveContext("HUMAN", "1", "integration-test"),
            )
        missing = User(
            id=999999,
            login="missing",
            name="missing",
            password_hash="unused",
            role="ADMIN_OPERATOR",
            active=True,
            permissions=[],
        )
        with pytest.raises(ObjectAuthorizationError):
            create_vehicle_type(
                db,
                "Missing principal",
                True,
                False,
                False,
                principal=missing,
                context=save_context(missing),
            )

        forged = User(
            id=viewer.id,
            login="forged",
            name="forged",
            password_hash="unused",
            role="ADMIN_OPERATOR",
            active=True,
            permissions=list(map(str, Permission)),
        )
        with pytest.raises(ObjectAuthorizationError):
            create_vehicle_type(
                db,
                "Forged authority",
                True,
                False,
                False,
                principal=forged,
                context=save_context(forged),
            )

        with pytest.raises(ObjectAuthorizationError, match="does not match"):
            create_vehicle_type(
                db,
                "Foreign author",
                True,
                False,
                False,
                principal=type_editor,
                context=SaveContext("HUMAN", str(admin.id), "integration-test"),
            )
        with pytest.raises(ObjectAuthorizationError, match="does not match"):
            create_vehicle_type(
                db,
                "System bypass",
                True,
                False,
                False,
                principal=admin,
                context=SaveContext("SYSTEM", str(admin.id), "integration-test"),
            )

        created = create_vehicle_type(
            db,
            "Authorized TYPE",
            True,
            False,
            False,
            principal=type_editor,
            context=save_context(type_editor),
        )
        admin_created = create_vehicle_type(
            db,
            "Admin TYPE",
            True,
            False,
            False,
            principal=admin,
            context=save_context(admin),
        )
        for principal in (type_editor, vehicle_viewer):
            with pytest.raises(ObjectAuthorizationError):
                create_vehicle(
                    db,
                    f"Denied VEH {principal.login}",
                    "A123BC86",
                    type_id=created.id,
                    principal=principal,
                    context=save_context(principal),
                )
        vehicle = create_vehicle(
            db,
            "Authorized VEH",
            "A123BC86",
            type_id=created.id,
            principal=vehicle_editor,
            context=save_context(vehicle_editor),
        )
        db.commit()
        assert (created.version, admin_created.version, vehicle.version) == (1, 1, 1)
        assert db.scalar(select(func.count()).select_from(VehicleType)) == 2
        events = db.scalars(
            select(HistoryEvent).order_by(HistoryEvent.event_id)
        ).all()
        assert [(event.actor_kind, event.actor_id) for event in events] == [
            ("HUMAN", str(type_editor.id)),
            ("HUMAN", str(admin.id)),
            ("HUMAN", str(vehicle_editor.id)),
        ]

        type_editor.permissions = [str(Permission.ARM2_TYPE_VIEW)]
        db.commit()
        for expected_version in (created.version, created.version + 99):
            with pytest.raises(ObjectAuthorizationError):
                update_vehicle_type(
                    db,
                    created.id,
                    expected_version,
                    created.name,
                    True,
                    False,
                    False,
                    principal=type_editor,
                    context=save_context(type_editor),
                )
        db.refresh(created)
        assert created.version == 1
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == 3

        type_editor.permissions = [
            str(Permission.ARM2_TYPE_VIEW),
            str(Permission.ARM2_TYPE_EDIT),
        ]
        db.commit()
        unchanged = update_vehicle_type(
            db,
            created.id,
            1,
            created.name,
            True,
            False,
            False,
            principal=type_editor,
            context=save_context(type_editor),
        )
        assert unchanged.version == 1
        with pytest.raises(ObjectVersionConflictError):
            update_vehicle_type(
                db,
                created.id,
                99,
                created.name,
                True,
                False,
                False,
                principal=type_editor,
                context=save_context(type_editor),
            )
        db.rollback()
    engine.dispose()


def test_type_uses_common_save_with_mandatory_atomic_history(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)

    with Session(engine) as db:
        principal = add_admin(db)
        vehicle_type = create_vehicle_type(
            db,
            "Самосвал",
            True,
            None,
            False,
            principal=principal,
            context=save_context(principal),
        )
        db.commit()
        principal_id = principal.id
        assert (vehicle_type.id, vehicle_type.version, vehicle_type.status) == (
            "TYPE-000001",
            1,
            "REVIEW",
        )

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        updated = update_vehicle_type(
            db,
            vehicle_type.id,
            1,
            "Самосвал",
            True,
            False,
            False,
            principal=principal,
            context=save_context(principal),
        )
        db.commit()
        assert (updated.version, updated.status) == (2, "CONFIRMED")

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        events = db.scalars(
            select(HistoryEvent).where(HistoryEvent.object_id == vehicle_type.id)
        ).all()
        assert len(events) == 2
        assert events[0].changes["requires_calibration"] == {"before": None, "after": None}
        assert events[0].changes["requires_wialon"] == {"before": None, "after": False}
        assert events[1].changes == {
            "requires_calibration": {"before": None, "after": False},
            "status": {"before": "REVIEW", "after": "CONFIRMED"},
            "version": {"before": 1, "after": 2},
        }

        no_op = update_vehicle_type(
            db,
            vehicle_type.id,
            2,
            "Самосвал",
            True,
            False,
            False,
            principal=principal,
            context=save_context(principal),
        )
        db.commit()
        assert no_op.version == 2
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == 2

    def fail_history(*args, **kwargs):
        raise RuntimeError("history unavailable")

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(RuntimeError, match="history unavailable"):
            create_object(
                db,
                object_type="TYPE",
                values={
                    "name": "Без истории",
                    "requires_sts": True,
                    "requires_calibration": False,
                    "requires_wialon": False,
                },
                principal=principal,
                context=save_context(principal),
                history_writer=fail_history,
            )
        assert db.scalar(select(VehicleType).where(VehicleType.name == "Без истории")) is None
        db.commit()

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(ObjectRegistryError, match="unknown: secret"):
            create_object(
                db,
                object_type="TYPE",
                values={
                    "name": "Опасное поле",
                    "requires_sts": True,
                    "requires_calibration": False,
                    "requires_wialon": False,
                    "secret": "token",
                },
                principal=principal,
                context=save_context(principal),
            )
        with pytest.raises(ObjectRegistryError, match="actor_id"):
            create_vehicle_type(
                db,
                "Нет автора",
                True,
                False,
                False,
                principal=principal,
                context=SaveContext("HUMAN", " ", "integration-test"),
            )
        db.rollback()

    engine.dispose()


@dataclass
class FakeRecord:
    id: str
    name: str
    enabled: bool | None
    status: str
    version: int


class FakeSession:
    def __init__(self, principal):
        self.principal = principal

    @property
    def no_autoflush(self):
        return nullcontext()

    def get(self, model, object_id, populate_existing=False):
        if model is User and object_id == self.principal.id:
            return self.principal
        return None

    def begin_nested(self):
        return nullcontext()

    def flush(self):
        pass


class FakeAdapter:
    def __init__(self):
        self.records = {}
        self.sequence = 0

    def normalize(self, values):
        name = values["name"].strip()
        if not name:
            raise ValueError("fake name is required")
        enabled = values["enabled"]
        return {"name": name, "enabled": enabled}

    def create_record(self, db, values):
        self.sequence += 1
        record = FakeRecord(f"FAKE-{self.sequence:06d}", version=1, **values)
        self.records[record.id] = record
        return record

    def lock_record(self, db, object_id):
        return self.records.get(object_id)

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)


def fake_status(values):
    return "READY" if values["enabled"] is not None else "REVIEW"


def test_second_isolated_object_reuses_the_same_save_algorithm():
    adapter = FakeAdapter()
    registry = ObjectRegistry()
    registry.register(
        ObjectDefinition(
            "FAKE",
            "FAKE",
            {"name": FieldSpec((str,)), "enabled": FieldSpec((bool,), nullable=True)},
            {
                "name": FieldSpec((str,)),
                "enabled": FieldSpec((bool,), nullable=True),
                "status": FieldSpec((str,)),
                "version": FieldSpec((int,)),
            },
            adapter=adapter,
            status_field="status",
            status_rule=fake_status,
            edit_permission=Permission.ARM2_TYPE_EDIT,
        )
    )
    events = []

    def fake_history_writer(db, **event):
        events.append(event)
        return event

    principal = User(
        id=1,
        login="fake-admin",
        name="fake-admin",
        password_hash="unused",
        role="ADMIN_OPERATOR",
        active=True,
        permissions=[],
    )
    session = FakeSession(principal)
    context = save_context(principal)
    created = create_object(
        session,
        object_type="FAKE",
        values={"name": "  Test object  ", "enabled": None},
        principal=principal,
        context=context,
        registry=registry,
        history_writer=fake_history_writer,
    )
    assert (created.record.id, created.record.status, created.record.version) == (
        "FAKE-000001",
        "REVIEW",
        1,
    )
    updated = update_object(
        session,
        object_type="FAKE",
        object_id="FAKE-000001",
        expected_version=1,
        values={"name": "Test object", "enabled": False},
        principal=principal,
        context=context,
        registry=registry,
        history_writer=fake_history_writer,
    )
    assert (updated.record.status, updated.record.version) == ("READY", 2)
    assert events[1]["changes"] == {
        "enabled": {"before": None, "after": False},
        "status": {"before": "REVIEW", "after": "READY"},
        "version": {"before": 1, "after": 2},
    }
    no_op = update_object(
        session,
        object_type="FAKE",
        object_id="FAKE-000001",
        expected_version=2,
        values={"name": "Test object", "enabled": False},
        principal=principal,
        context=context,
        registry=registry,
        history_writer=fake_history_writer,
    )
    assert not no_op.changed
    assert len(events) == 2


def test_common_save_rejects_object_without_registered_edit_permission():
    adapter = FakeAdapter()
    registry = ObjectRegistry()
    registry.register(
        ObjectDefinition(
            "FAKE",
            "FAKE",
            {"name": FieldSpec((str,)), "enabled": FieldSpec((bool,), nullable=True)},
            {
                "name": FieldSpec((str,)),
                "enabled": FieldSpec((bool,), nullable=True),
                "status": FieldSpec((str,)),
                "version": FieldSpec((int,)),
            },
            adapter=adapter,
            status_field="status",
            status_rule=fake_status,
        )
    )
    principal = User(
        id=1,
        login="fake-admin",
        name="fake-admin",
        password_hash="unused",
        role="ADMIN_OPERATOR",
        active=True,
        permissions=[],
    )
    with pytest.raises(ObjectAuthorizationError, match="No edit permission"):
        create_object(
            FakeSession(principal),
            object_type="FAKE",
            values={"name": "Must not save", "enabled": True},
            principal=principal,
            context=save_context(principal),
            registry=registry,
            history_writer=lambda *args, **kwargs: None,
        )
    assert adapter.sequence == 0


def test_adapter_cannot_supply_platform_status():
    class InvalidAdapter(FakeAdapter):
        def normalize(self, values):
            normalized = super().normalize(values)
            normalized["status"] = "READY"
            return normalized

    registry = ObjectRegistry()
    registry.register(
        ObjectDefinition(
            "FAKE",
            "FAKE",
            {"name": FieldSpec((str,)), "enabled": FieldSpec((bool,), nullable=True)},
            {
                "name": FieldSpec((str,)),
                "enabled": FieldSpec((bool,), nullable=True),
                "status": FieldSpec((str,)),
                "version": FieldSpec((int,)),
            },
            adapter=InvalidAdapter(),
            status_field="status",
            status_rule=fake_status,
            edit_permission=Permission.ARM2_TYPE_EDIT,
        )
    )
    with pytest.raises(ObjectRegistryError, match="must not set platform status"):
        create_object(
            FakeSession(
                User(
                    id=1,
                    login="fake-admin",
                    name="fake-admin",
                    password_hash="unused",
                    role="ADMIN_OPERATOR",
                    active=True,
                    permissions=[],
                )
            ),
            object_type="FAKE",
            values={"name": "Invalid status source", "enabled": True},
            principal=User(
                id=1,
                login="fake-admin",
                name="fake-admin",
                password_hash="unused",
                role="ADMIN_OPERATOR",
                active=True,
                permissions=[],
            ),
            context=SaveContext("HUMAN", "1", "integration-test"),
            registry=registry,
            history_writer=lambda *args, **kwargs: None,
        )


def test_explicit_adapter_initialization_is_import_order_independent(database_url):
    script = r'''
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend"))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.arm2.object_save import SaveContext, create_object
from app.arm2.object_registry import (
    OBJECT_REGISTRY,
    ObjectRegistryError,
    initialize_object_adapters,
)
from app.models import User

assert "app.arm2.vehicle_type_store" not in sys.modules
assert OBJECT_REGISTRY.require("TYPE").adapter is None
initialize_object_adapters()
adapter = OBJECT_REGISTRY.require("TYPE").adapter
assert adapter is not None
initialize_object_adapters()
assert OBJECT_REGISTRY.require("TYPE").adapter is adapter

engine = create_engine(os.environ["TEST_DATABASE_URL"])
with Session(engine) as db:
    principal = User(
        login="isolated-arm2-admin",
        name="isolated-arm2-admin",
        password_hash="unused",
        role="ADMIN_OPERATOR",
        active=True,
        permissions=[],
    )
    db.add(principal)
    db.flush()
    result = create_object(
        db,
        object_type="TYPE",
        values={
            "name": "Изолированный TYPE",
            "requires_sts": True,
            "requires_calibration": False,
            "requires_wialon": False,
        },
        principal=principal,
        context=SaveContext("HUMAN", str(principal.id), "integration-test"),
    )
    db.commit()
    assert result.record.id.startswith("TYPE-")
    assert result.event.object_id == result.record.id

with Session(engine) as db:
    from app.arm2.history_models import HistoryEvent
    principal = db.scalar(select(User).where(User.login == "isolated-arm2-admin"))
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) >= 1
    vehicle_adapter = OBJECT_REGISTRY.require("VEH").adapter
    assert vehicle_adapter is not None
    initialize_object_adapters()
    assert OBJECT_REGISTRY.require("VEH").adapter is vehicle_adapter
    vehicle = create_object(
        db,
        object_type="VEH",
        values={
            "name": "Изолированная машина",
            "plate_number": "А123ВС86",
            "type_id": None,
            "accounting_state": "Действующая",
            "model": None,
            "wialon_unit_id": None,
        },
        principal=principal,
        context=SaveContext("HUMAN", str(principal.id), "integration-test"),
    )
    db.commit()
    assert vehicle.record.id.startswith("VEH-")
    assert vehicle.event.object_type == "VEH"
    assert vehicle.event.object_id == vehicle.record.id
engine.dispose()
'''
    environment = os.environ.copy()
    environment["TEST_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
