"""Verify browser evidence in the new DB; invalid raw SQL always rolls back."""
import json

from policy import ROOT, deny_external_network, readiness, validate_environment

validate_environment()
deny_external_network()

from app.db import engine  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

evidence = json.loads((ROOT / "evidence/browser.json").read_text())
with engine().connect() as connection:
    readiness(connection)
    tables = connection.execute(text("SELECT table_name FROM information_schema.columns WHERE column_name='requires_sts' AND table_schema='public'" )).scalars().all()
    assert len(tables) == 1
    row = connection.execute(text(f"SELECT name,version FROM {tables[0]} WHERE id=:id"), {"id": evidence["id"]}).one()
    assert tuple(row) == (evidence["name"], 1)
    event = connection.execute(text("SELECT object_id,operation,version_after FROM arm2_history_events WHERE event_id=:id"), {"id": evidence["event_id"]}).one()
    assert tuple(event) == (evidence["id"], "CREATED", 1)
    connection.rollback()
    checked = 0
    for item in evidence["signer"]:
        table = {"counterparties": "arm2_counterparties", "organizations": "arm2_organizations"}[item["kind"]]
        for signer in [item["foreign_id"], item["foreign_id"].split('-')[0] + '-999999999']:
            try:
                connection.execute(text(f"UPDATE {table} SET primary_signer_id=:signer WHERE id=:id"), {"id": item["id"], "signer": signer})
            except IntegrityError as error:
                assert error.orig.sqlstate == "23503"
                checked += 1
            else:
                raise AssertionError("Database accepted invalid signer")
            finally:
                connection.rollback()
        row = connection.execute(text(f"SELECT primary_signer_id,version FROM {table} WHERE id=:id"), {"id": item["id"]}).one()
        assert tuple(row) == (None, 1)
        connection.rollback()
assert checked == 4
(ROOT / "evidence/database.json").write_text(json.dumps({"record_id": evidence["id"], "record_history_persisted": True,
                                                        "raw_sql_fk_rejected_rolled_back": checked}), encoding="utf-8")
print("PASS: browser record/history persisted in PostgreSQL; four raw-SQL signer FK violations rolled back")
