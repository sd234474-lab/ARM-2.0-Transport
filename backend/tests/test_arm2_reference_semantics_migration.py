"""Check the real 0075/0076 catalog and preserve populated history."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from test_arm2_bulk import accept as import_row
from test_arm2_bulk import upload
from test_arm2_calibration_approval import letter, prepare
from test_arm2_deletion import execute, mark, ok, preview
from test_arm2_document_api import api as api
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import qa as qa
from test_arm2_questionnaire_workflow import apply as apply_questionnaire
from test_arm2_questionnaire_workflow import choose as choose_questionnaire
from test_arm2_questionnaire_workflow import doc_kind, transport
from test_arm2_questionnaire_workflow import upload as upload_questionnaire
from test_arm2_questionnaire_workflow import workbook as questionnaire_workbook

from alembic import command
from app.config import settings

TARGETS = {
    ("arm2_bulk_receipts", ("vehicle_id",), "arm2_vehicles"),
    ("arm2_questionnaire_exports", ("cp_id",), "arm2_counterparties"),
    ("arm2_document_revision_contexts", ("_cp",), "arm2_counterparties"),
    ("arm2_document_revision_contexts", ("_org",), "arm2_organizations"),
    ("arm2_document_revision_contexts", ("_veh",), "arm2_vehicles"),
}
TABLES = (
    "arm2_bulk_receipts", "arm2_questionnaire_exports", "arm2_questionnaire_receipts",
    "arm2_document_revisions", "arm2_document_revision_files", "arm2_document_revision_contexts",
    "arm2_document_files", "arm2_document_file_acceptances", "arm2_history_events",
    "arm2_approval_attempts", "arm2_deletion_batches", "arm2_vehicles", "arm2_counterparties",
    "arm2_approval_requests", "arm2_approval_decisions", "arm2_questionnaire_drafts", "arm2_questionnaire_uploads",
)


def config():
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def inventory(engine):
    return {
        (table, tuple(fk["constrained_columns"]), fk["referred_table"]): (
            fk["name"], tuple(fk["referred_columns"]), dict(fk["options"]),
        )
        for (_, table), keys in inspect(engine).get_multi_foreign_keys(schema="public").items() for fk in keys
    }


def snapshot(engine):
    with engine.connect() as db:
        return {table: db.exec_driver_sql(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY 1").scalars().all()
                for table in TABLES}


def test_five_fk_delta_and_populated_roundtrip(qa, monkeypatch):
    c, db, _, parents, _, disk = qa
    database = db.get_bind()
    monkeypatch.setenv("ARM2_TABLE_SIGNING_KEY", "isolated-reference-semantics-key")
    settings.cache_clear()
    command.downgrade(config(), "0075")
    original = pdf("migration")
    url = create(qa, "veh")
    ok(accept(c, url, 1, [stage(c, url, original)]))
    workbook = upload(c, "Госномер\nА811ВС86")
    import_row(c, workbook)
    ok(c.post("/api/arm2/questionnaires/exports/" + parents["cp"], json={"format": "xlsx"}))
    sts_kind = doc_kind(c)
    form = questionnaire_workbook({"B18": "+7 (900) 111-22-33"}, [{"E": "A123BC86", "P": "sts.pdf"}])
    draft = upload_questionnaire(c, transport(form, {"sts.pdf": pdf("questionnaire")}), True)
    draft = choose_questionnaire(c, draft, cp_target=parents["cp"], cp_fields=["phone"],
                                 documents={"v:5:sts": {"kind_id": sts_kind, "accept": True}})
    result, _ = apply_questionnaire(c, draft)
    assert result["event_ids"]
    _, response = letter(c, prepare(qa))
    assert ok(response, 201)["request_id"]
    type_id = ok(c.post("/api/arm2/types", json={
        "name": "Для проверки журнала удаления", "plate_category": "Автомобильный",
        "requires_sts": True, "requires_calibration": False, "requires_wialon": False,
    }), 201)["id"]
    mark(c, "TYPE", type_id)
    checked = preview(c, ("TYPE", type_id))
    assert checked["can_execute"]
    ok(execute(c, checked["id"]))
    db.rollback()
    before = snapshot(database)
    assert not [table for table in TABLES if not before[table]]
    fks = inventory(database)
    assert TARGETS <= fks.keys()
    command.upgrade(config(), "0076")
    assert inventory(database) == {k: v for k, v in fks.items() if k not in TARGETS}
    assert snapshot(database) == before
    assert disk.location(disk.uploads[0]).read_bytes() == original
    command.downgrade(config(), "0075")
    assert inventory(database) == fks and snapshot(database) == before


def test_inconsistent_manifest_stops_migration_before_fk_changes(qa):
    c, db, _, _, _, _ = qa
    database = db.get_bind()
    command.downgrade(config(), "0075")
    url = create(qa, "veh")
    ok(accept(c, url, 1, [stage(c, url, pdf("mismatched"))]))
    revision = bundle(c, url)["current_revision_id"]
    db.rollback()
    with database.begin() as conn:
        for row in conn.execute(text("""
            SELECT tgname FROM pg_trigger WHERE tgrelid='arm2_document_revision_files'::regclass
              AND NOT tgisinternal
        """)):
            assert row.tgname.isidentifier()
            conn.exec_driver_sql(f"ALTER TABLE arm2_document_revision_files DISABLE TRIGGER {row.tgname}")
        conn.execute(text("DELETE FROM arm2_document_revision_files WHERE revision_id=:id"), {"id": revision})
    before = snapshot(database)
    fks = inventory(database)
    with pytest.raises(RuntimeError, match="inconsistent document revision"):
        command.upgrade(config(), "0076")
    assert inventory(database) == fks and snapshot(database) == before
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0075"


def test_new_history_refuses_downgrade_without_destroying_receipt(qa):
    c, db, _, _, _, _ = qa
    draft = upload(c, "Госномер\nА810ВС86")
    import_row(c, draft)
    database = db.get_bind()
    before = snapshot(database)
    with pytest.raises(RuntimeError, match="new or retired historical evidence"):
        command.downgrade(config(), "0075")
    assert snapshot(database) == before
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0076"
