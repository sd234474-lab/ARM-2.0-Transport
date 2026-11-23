"""Logical zero-file DOCUMENT, typed parent FKs and shared history registration."""

import sqlalchemy as sa

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None
OLD_TYPES = ("TYPE", "VEH", "CP", "ORG", "CPREP", "ORGREP", "DOC-KIND")


def constraints(types):
    op.create_check_constraint(
        "ck_arm2_history_object_type",
        "arm2_history_events",
        "object_type IN (" + ",".join(f"'{t}'" for t in types) + ")",
    )
    op.create_check_constraint(
        "ck_arm2_history_object_id",
        "arm2_history_events",
        " OR ".join(f"(object_type='{t}' AND object_id ~ '^{t}-[0-9]{{6,}}$')" for t in types),
    )


def drop_checks():
    for name in ("ck_arm2_history_object_type", "ck_arm2_history_object_id"):
        op.drop_constraint(name, "arm2_history_events", type_="check")


def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence("arm2_document_id_seq", start=1)))
    op.create_table(
        "arm2_documents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("parent_type", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=False),
        sa.Column(
            "kind_ref",
            sa.String(),
            sa.ForeignKey("arm2_document_kinds.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String()),
        sa.Column("archive_state", sa.String(), nullable=False, server_default=sa.text("'Неархивный'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        *[
            sa.Column(
                column,
                sa.String(),
                sa.Computed(f"CASE WHEN parent_type='{ptype}' THEN parent_id END", persisted=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
            )
            for column, ptype, table in [
                ("_cp", "CP", "arm2_counterparties"),
                ("_org", "ORG", "arm2_organizations"),
                ("_veh", "VEH", "arm2_vehicles"),
            ]
        ],
        sa.CheckConstraint("id ~ '^DOCUMENT-[0-9]{6,}$'", name="ck_arm2_documents_id"),
        sa.CheckConstraint(
            "(parent_type='CP' AND parent_id ~ '^CP-[0-9]{6,}$') OR (parent_type='ORG' AND parent_id ~ '^ORG-[0-9]{6,}$') OR (parent_type='VEH' AND parent_id ~ '^VEH-[0-9]{6,}$')",
            name="ck_arm2_documents_parent",
        ),
        sa.CheckConstraint(
            "name=btrim(name) AND name<>'' AND name !~ '^[[:space:]]|[[:space:]]$'",
            name="ck_arm2_documents_name",
        ),
        sa.CheckConstraint("archive_state='Неархивный'", name="ck_arm2_documents_archive"),
        sa.CheckConstraint("version>0", name="ck_arm2_documents_version"),
    )
    op.create_index(
        "ix_arm2_documents_parent_kind",
        "arm2_documents",
        ["parent_type", "parent_id", "kind_ref", "archive_state"],
    )
    drop_checks()
    constraints((*OLD_TYPES, "DOCUMENT"))


def downgrade():
    db = op.get_bind()
    db.execute(sa.text("LOCK TABLE arm2_documents, arm2_history_events IN ACCESS EXCLUSIVE MODE"))
    if db.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM arm2_documents) OR EXISTS(SELECT 1 FROM arm2_history_events WHERE object_type='DOCUMENT' OR related_objects @> '[{\"object_type\":\"DOCUMENT\"}]'::jsonb)"
        )
    ):
        raise RuntimeError("DOCUMENT data/history exists; downgrade refused without deleting data")
    drop_checks()
    constraints(OLD_TYPES)
    op.drop_table("arm2_documents")
    op.execute(sa.schema.DropSequence(sa.Sequence("arm2_document_id_seq")))
