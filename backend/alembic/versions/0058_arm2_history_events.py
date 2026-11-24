"""Add the shared ARM 2.0 history event store."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "arm2_history_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("object_type", sa.String(), nullable=False),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String()),
        sa.Column("version_before", sa.Integer()),
        sa.Column("version_after", sa.Integer()),
        sa.Column("changes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "related_objects",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.CheckConstraint("object_type IN ('TYPE', 'VEH')", name="ck_arm2_history_object_type"),
        sa.CheckConstraint(
            "(object_type = 'TYPE' AND object_id ~ '^TYPE-[0-9]{6,}$') OR "
            "(object_type = 'VEH' AND object_id ~ '^VEH-[0-9]{6,}$')",
            name="ck_arm2_history_object_id",
        ),
        sa.CheckConstraint("operation IN ('CREATED', 'UPDATED')", name="ck_arm2_history_operation"),
        sa.CheckConstraint("btrim(actor_kind) <> ''", name="ck_arm2_history_actor_kind"),
        sa.CheckConstraint("btrim(actor_id) <> ''", name="ck_arm2_history_actor_id"),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_arm2_history_source"),
        sa.CheckConstraint(
            "correlation_id IS NULL OR btrim(correlation_id) <> ''",
            name="ck_arm2_history_correlation",
        ),
        sa.CheckConstraint(
            "version_before IS NULL OR version_before > 0",
            name="ck_arm2_history_version_before",
        ),
        sa.CheckConstraint(
            "version_after IS NULL OR version_after > 0",
            name="ck_arm2_history_version_after",
        ),
        sa.CheckConstraint(
            "(operation = 'CREATED' AND version_before IS NULL AND version_after = 1) OR "
            "(operation = 'UPDATED' AND version_before IS NOT NULL "
            "AND version_after IS NOT NULL AND version_after > version_before)",
            name="ck_arm2_history_versions",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(changes) = 'object' AND changes <> '{}'::jsonb",
            name="ck_arm2_history_changes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(related_objects) = 'array'",
            name="ck_arm2_history_related_objects",
        ),
    )
    op.create_index(
        "ix_arm2_history_object_chronology",
        "arm2_history_events",
        ["object_type", "object_id", "occurred_at", "event_id"],
    )


def downgrade():
    op.drop_index("ix_arm2_history_object_chronology", table_name="arm2_history_events")
    op.drop_table("arm2_history_events")
