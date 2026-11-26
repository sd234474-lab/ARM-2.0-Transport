"""Preserve deletion audit and permit scoped retirement of immutable B3 artifacts."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = depends_on = None

OLD_VERSIONS = """(operation = 'CREATED' AND version_before IS NULL AND version_after = 1)
    OR (operation = 'UPDATED' AND version_before IS NOT NULL
        AND version_after IS NOT NULL AND version_after > version_before)"""
NEW_VERSIONS = OLD_VERSIONS + """
    OR (operation IN ('MARKED', 'UNMARKED') AND version_before IS NOT NULL
        AND version_after IS NOT NULL AND version_after = version_before)
    OR (operation = 'DELETED' AND version_before IS NOT NULL AND version_after IS NULL)"""


def history_contract(operations, versions):
    op.drop_constraint("ck_arm2_history_operation", "arm2_history_events", type_="check")
    op.drop_constraint("ck_arm2_history_versions", "arm2_history_events", type_="check")
    op.create_check_constraint("ck_arm2_history_operation", "arm2_history_events", operations)
    op.create_check_constraint("ck_arm2_history_versions", "arm2_history_events", versions)


def upgrade():
    op.create_table(
        "arm2_deletion_marks",
        sa.Column("object_type", sa.String(20), primary_key=True),
        sa.Column("object_id", sa.String(80), primary_key=True),
        sa.Column("command_id", sa.String(36), nullable=False, unique=True),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "arm2_deletion_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_tx", sa.String(24)),
        sa.Column("artifacts", JSONB),
    )
    op.create_index("ix_arm2_deletion_journal", "arm2_deletion_batches", ["actor_id", "executed_at"])
    op.create_index("ix_arm2_deletion_execution_tx", "arm2_deletion_batches", ["execution_tx"])
    op.add_column("arm2_background_operations", sa.Column("not_before", sa.DateTime(timezone=True)))
    history_contract("operation IN ('CREATED', 'UPDATED', 'MARKED', 'UNMARKED', 'DELETED')", NEW_VERSIONS)
    op.execute("""
        CREATE OR REPLACE FUNCTION arm2_immutable_document_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE artifact_key text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                artifact_key := CASE TG_TABLE_NAME
                    WHEN 'arm2_document_revision_contexts' THEN to_jsonb(OLD)->>'revision_id'
                    WHEN 'arm2_document_revision_files' THEN
                        (to_jsonb(OLD)->>'revision_id') || ':' || (to_jsonb(OLD)->>'file_id')
                    WHEN 'arm2_document_file_corrections' THEN to_jsonb(OLD)->>'file_id'
                    ELSE to_jsonb(OLD)->>'id'
                END;
                IF EXISTS (
                    SELECT 1 FROM arm2_deletion_batches b
                    WHERE b.execution_tx=txid_current()::text AND b.executed_at IS NOT NULL
                    AND (b.artifacts->TG_TABLE_NAME) ? artifact_key
                ) THEN
                    RETURN OLD;
                END IF;
            END IF;
            RAISE EXCEPTION 'Accepted document artifacts are immutable';
        END $$;
        CREATE FUNCTION arm2_guard_deletion_audit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.executed_at IS NOT NULL
                OR NEW.id <> OLD.id OR NEW.actor_id <> OLD.actor_id
                OR NEW.created_at <> OLD.created_at OR NEW.snapshot IS DISTINCT FROM OLD.snapshot THEN
                RAISE EXCEPTION 'ARM2 deletion audit is immutable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER arm2_guard_deletion_audit BEFORE UPDATE OR DELETE ON arm2_deletion_batches
        FOR EACH ROW EXECUTE FUNCTION arm2_guard_deletion_audit();
    """)


def downgrade():
    if op.get_bind().scalar(sa.text("""SELECT EXISTS (SELECT 1 FROM arm2_deletion_batches WHERE executed_at IS NOT NULL)
        OR EXISTS (SELECT 1 FROM arm2_deletion_marks)
        OR EXISTS (SELECT 1 FROM arm2_history_events WHERE operation IN ('MARKED','UNMARKED','DELETED'))""")):
        raise RuntimeError("0075 downgrade refused: deletion audit exists")
    op.execute("""
        CREATE OR REPLACE FUNCTION arm2_immutable_document_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Accepted document artifacts are immutable'; END $$;
        DROP TRIGGER arm2_guard_deletion_audit ON arm2_deletion_batches;
        DROP FUNCTION arm2_guard_deletion_audit();
    """)
    history_contract("operation IN ('CREATED', 'UPDATED')", OLD_VERSIONS)
    op.drop_column("arm2_background_operations", "not_before")
    op.drop_table("arm2_deletion_batches")
    op.drop_table("arm2_deletion_marks")
