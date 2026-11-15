"""Register the three-field DOC-KIND object without changing existing data."""
import sqlalchemy as sa

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

OLD_TYPES = ("TYPE", "VEH", "CP", "ORG", "CPREP", "ORGREP")


def history_constraints(types):
    op.create_check_constraint(
        "ck_arm2_history_object_type", "arm2_history_events",
        "object_type IN (" + ",".join(f"'{kind}'" for kind in types) + ")",
    )
    op.create_check_constraint(
        "ck_arm2_history_object_id", "arm2_history_events",
        " OR ".join(f"(object_type = '{kind}' AND object_id ~ '^{kind}-[0-9]{{6,}}$')" for kind in types),
    )


def drop_history_constraints():
    for name in ("ck_arm2_history_object_type", "ck_arm2_history_object_id"):
        op.drop_constraint(name, "arm2_history_events", type_="check")


def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence("arm2_document_kind_id_seq", start=1)))
    op.create_table(
        "arm2_document_kinds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("id ~ '^DOC-KIND-[0-9]{6,}$'", name="ck_arm2_document_kinds_id"),
        sa.CheckConstraint(
            "name = btrim(name) AND name <> '' AND name !~ '^[[:space:]]|[[:space:]]$'",
            name="ck_arm2_document_kinds_name",
        ),
        sa.CheckConstraint("version > 0", name="ck_arm2_document_kinds_version"),
        sa.UniqueConstraint("name", name="uq_arm2_document_kinds_name"),
    )
    drop_history_constraints()
    history_constraints((*OLD_TYPES, "DOC-KIND"))


def downgrade():
    # Never delete document kinds or their history just to make an old CHECK fit.
    connection = op.get_bind()
    connection.execute(sa.text(
        "LOCK TABLE arm2_document_kinds, arm2_history_events IN ACCESS EXCLUSIVE MODE"
    ))
    used = connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM arm2_document_kinds) OR EXISTS ("
        "SELECT 1 FROM arm2_history_events WHERE object_type = 'DOC-KIND' "
        "OR related_objects @> '[{\"object_type\":\"DOC-KIND\"}]'::jsonb)"
    ))
    if used:
        raise RuntimeError("DOC-KIND data/history exists; downgrade refused without deleting data")
    drop_history_constraints()
    history_constraints(OLD_TYPES)
    op.drop_table("arm2_document_kinds")
    op.execute(sa.schema.DropSequence(sa.Sequence("arm2_document_kind_id_seq")))
