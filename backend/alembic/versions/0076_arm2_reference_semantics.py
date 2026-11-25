"""Keep historical references independent of the lifetime of an ARM2 card."""

import hashlib
import json

import sqlalchemy as sa

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = depends_on = None

HISTORY_FKS = (
    ("arm2_bulk_receipts", "vehicle_id", "arm2_vehicles"),
    ("arm2_questionnaire_exports", "cp_id", "arm2_counterparties"),
    ("arm2_document_revision_contexts", "_cp", "arm2_counterparties"),
    ("arm2_document_revision_contexts", "_org", "arm2_organizations"),
    ("arm2_document_revision_contexts", "_veh", "arm2_vehicles"),
)
TYPED_IDS = (
    ("arm2_bulk_receipts", "vehicle_id", "VEH", "ck_arm2_bulk_receipt_vehicle"),
    ("arm2_questionnaire_exports", "cp_id", "CP", "ck_arm2_questionnaire_export_cp"),
)
HISTORY_ROWS = (
    ("arm2_bulk_receipts", "true"),
    ("arm2_questionnaire_exports", "true"),
    ("arm2_questionnaire_receipts", "true"),
    ("arm2_document_revision_contexts", "true"),
    ("arm2_approval_attempts", "result IS NOT NULL"),
    ("arm2_document_file_acceptances", "state = 'accepted'"),
)
GUARD = "arm2_guard_reference_history"


def foreign_keys(db):
    inspector = sa.inspect(db)
    sources = {source for source, _, _ in HISTORY_FKS}
    targets = {target for _, _, target in HISTORY_FKS}
    found = {}
    for table in sorted(sources):
        for fk in inspector.get_foreign_keys(table, schema="public"):
            if fk["referred_table"] not in targets or fk["referred_schema"] not in (None, "public"):
                continue
            columns = fk["constrained_columns"]
            key = (table, columns[0], fk["referred_table"])
            if (key not in HISTORY_FKS or key in found or len(columns) != 1
                    or fk["referred_columns"] != ["id"] or fk["options"].get("ondelete") != "RESTRICT"
                    or not fk["name"]):
                raise RuntimeError(f"0076 unexpected historical FK: {table}.{fk['name']}")
            found[key] = fk["name"]
    if set(found) != set(HISTORY_FKS):
        raise RuntimeError("0076 expected exactly five historical foreign keys")
    return found


def history_fingerprint(db):
    digest = hashlib.sha256()
    for table, where in HISTORY_ROWS:
        digest.update(table.encode())
        for value in db.exec_driver_sql(
            f"SELECT to_jsonb(r)::text FROM {table} r WHERE {where} ORDER BY 1"
        ).scalars():
            digest.update(value.encode())
            digest.update(b"\n")
    return digest.hexdigest()


def validate_history(db):
    for table, column, prefix, _ in TYPED_IDS:
        if db.exec_driver_sql(f"SELECT EXISTS (SELECT FROM {table} WHERE {column} !~ '^{prefix}-[0-9]{{6,}}$')").scalar():
            raise RuntimeError(f"0076 invalid historical ID in {table}.{column}")
    if db.scalar(sa.text("""
        SELECT r.command_id FROM arm2_bulk_receipts r
        JOIN arm2_vehicles v ON v.id = r.vehicle_id
        LEFT JOIN arm2_history_events h ON h.event_id = r.event_id
        WHERE r.version_after <= 0 OR v.version < r.version_after OR (
            r.event_id IS NOT NULL AND (
                h.object_type IS DISTINCT FROM 'VEH' OR h.object_id IS DISTINCT FROM r.vehicle_id
                OR h.actor_id IS DISTINCT FROM r.user_id::text OR h.source IS DISTINCT FROM 'arm2-table'
                OR h.correlation_id IS DISTINCT FROM r.command_id OR h.version_after IS DISTINCT FROM r.version_after
            )
        )
        LIMIT 1
    """)):
        raise RuntimeError("0076 inconsistent vehicle import receipt evidence")
    if db.scalar(sa.text("""
        SELECT r.id FROM arm2_document_revisions r
        LEFT JOIN arm2_document_revision_contexts c ON c.revision_id = r.id AND c.document_id = r.document_id
        WHERE c.revision_id IS NULL OR r.file_ids IS DISTINCT FROM (
            SELECT coalesce(jsonb_agg(f.file_id ORDER BY f.position), '[]'::jsonb)
            FROM arm2_document_revision_files f
            WHERE f.revision_id = r.id AND f.document_id = r.document_id
        )
        LIMIT 1
    """)):
        raise RuntimeError("0076 inconsistent document revision context or ordered file manifest")


def upgrade():
    db = op.get_bind()
    op.execute("LOCK TABLE " + ", ".join(sorted({table for table, _ in HISTORY_ROWS} | {
        "arm2_document_revisions", "arm2_document_revision_files",
    })) + " IN SHARE ROW EXCLUSIVE MODE")
    names = foreign_keys(db)
    validate_history(db)
    baseline = json.dumps({"history": history_fingerprint(db), "fk_names": [names[key] for key in HISTORY_FKS]})
    for (table, _, _), name in names.items():
        op.drop_constraint(name, table, schema="public", type_="foreignkey")
    for table, column, prefix, constraint in TYPED_IDS:
        op.create_check_constraint(constraint, table, f"{column} ~ '^{prefix}-[0-9]{{6,}}$'")
    op.execute(f"""
        CREATE FUNCTION {GUARD}() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            previous jsonb := to_jsonb(OLD);
            following jsonb := to_jsonb(NEW);
        BEGIN
            IF TG_NARGS = 1 THEN
                IF TG_OP = 'DELETE' AND EXISTS (
                    SELECT 1 FROM arm2_deletion_batches b
                    WHERE b.execution_tx = txid_current()::text AND b.executed_at IS NOT NULL
                    AND (b.artifacts -> TG_TABLE_NAME) ? (previous ->> 'id')
                ) THEN
                    RETURN OLD;
                END IF;
                IF TG_OP = 'UPDATE' AND previous ->> 'result' IS NULL THEN
                    IF TG_ARGV[0] = 'approval' AND following ->> 'result' IS NOT NULL
                       AND previous - 'result' = following - 'result' THEN
                        RETURN NEW;
                    END IF;
                    IF TG_ARGV[0] = 'acceptance' AND previous ->> 'state' IN ('prepared', 'uploaded')
                       AND previous - ARRAY['plan','state','result'] = following - ARRAY['plan','state','result']
                       AND (previous -> 'plan') - 'uploaded' = (following -> 'plan') - 'uploaded'
                       AND ((following ->> 'result' IS NULL AND following ->> 'state' IN ('prepared','uploaded'))
                            OR (following ->> 'result' IS NOT NULL AND following ->> 'state' = 'accepted')) THEN
                        RETURN NEW;
                    END IF;
                END IF;
            END IF;
            RAISE EXCEPTION 'ARM2 historical evidence is immutable';
        END $$;
        COMMENT ON FUNCTION {GUARD}() IS '{baseline.replace("'", "''")}';
    """)
    for table, mode in (
        ("arm2_bulk_receipts", ""), ("arm2_questionnaire_exports", ""),
        ("arm2_questionnaire_receipts", ""), ("arm2_approval_attempts", "'approval'"),
        ("arm2_document_file_acceptances", "'acceptance'"),
    ):
        op.execute(f"CREATE TRIGGER {GUARD} BEFORE UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {GUARD}({mode})")


def downgrade():
    db = op.get_bind()
    tables = {table for table, _ in HISTORY_ROWS} | {target for _, _, target in HISTORY_FKS}
    op.execute(f"LOCK TABLE {', '.join(sorted(tables))} IN ACCESS EXCLUSIVE MODE")
    for table, column, target in HISTORY_FKS:
        if db.exec_driver_sql(f"SELECT EXISTS (SELECT FROM {table} s LEFT JOIN {target} t ON s.{column}=t.id "
                              f"WHERE s.{column} IS NOT NULL AND t.id IS NULL)").scalar():
            raise RuntimeError(f"0076 downgrade refused: retained history in {table}.{column}")
    baseline = json.loads(db.scalar(sa.text(f"SELECT obj_description('{GUARD}()'::regprocedure, 'pg_proc')")))
    if history_fingerprint(db) != baseline["history"]:
        raise RuntimeError("0076 downgrade refused: new or retired historical evidence")
    for table, _ in HISTORY_ROWS:
        if table != "arm2_document_revision_contexts":
            op.execute(f"DROP TRIGGER {GUARD} ON {table}")
    op.execute(f"DROP FUNCTION {GUARD}()")
    for table, _, _, constraint in TYPED_IDS:
        op.drop_constraint(constraint, table, type_="check")
    for (table, column, target), name in zip(HISTORY_FKS, baseline["fk_names"], strict=True):
        op.create_foreign_key(name, table, target, [column], ["id"], ondelete="RESTRICT")
