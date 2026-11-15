"""Store DOC-KIND parent applicability and logical-document multiplicity only."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade():
    # Neutral backfill only. No initial document kinds, version bumps or events.
    op.add_column("arm2_document_kinds", sa.Column(
        "applicable_object_types", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'[\"CP\",\"ORG\",\"VEH\"]'::jsonb"),
    ))
    op.add_column("arm2_document_kinds", sa.Column("max_current_documents", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_arm2_document_kinds_parents", "arm2_document_kinds",
        "applicable_object_types IN ('[\"CP\"]'::jsonb, '[\"ORG\"]'::jsonb, '[\"VEH\"]'::jsonb, "
        "'[\"CP\",\"ORG\"]'::jsonb, '[\"CP\",\"VEH\"]'::jsonb, '[\"ORG\",\"VEH\"]'::jsonb, "
        "'[\"CP\",\"ORG\",\"VEH\"]'::jsonb)",
    )
    op.create_check_constraint("ck_arm2_document_kinds_max_current", "arm2_document_kinds",
                               "max_current_documents IS NULL OR max_current_documents > 0")
    # Creation must supply explicit settings; the backfill is not a service default.
    op.alter_column("arm2_document_kinds", "applicable_object_types", server_default=None)


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE arm2_document_kinds, arm2_history_events IN ACCESS EXCLUSIVE MODE"))
    configured = connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM arm2_document_kinds "
        "WHERE applicable_object_types IS DISTINCT FROM '[\"CP\",\"ORG\",\"VEH\"]'::jsonb "
        "OR max_current_documents IS NOT NULL) OR EXISTS (SELECT 1 FROM arm2_history_events "
        "WHERE object_type='DOC-KIND' AND (changes ? 'applicable_object_types' OR changes ? 'max_current_documents'))"
    ))
    if configured:
        raise RuntimeError("DOC-KIND settings/history would be lost; downgrade refused without deleting data")
    op.drop_constraint("ck_arm2_document_kinds_max_current", "arm2_document_kinds", type_="check")
    op.drop_constraint("ck_arm2_document_kinds_parents", "arm2_document_kinds", type_="check")
    op.drop_column("arm2_document_kinds", "max_current_documents")
    op.drop_column("arm2_document_kinds", "applicable_object_types")
