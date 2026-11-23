"""Enforce primary signer existence and ownership without rewriting existing data."""

import sqlalchemy as sa

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

PAIRS = (
    ("arm2_counterparties", "nsi_counterparty_representatives", "cp", "cprep"),
    ("arm2_organizations", "nsi_organization_representatives", "org", "orgrep"),
)


def upgrade():
    connection = op.get_bind()
    # Hold both pairs stable through validation and constraint creation. Migration
    # is transactional on PostgreSQL; a diagnostic abort leaves all data intact.
    connection.execute(sa.text("LOCK TABLE " + ", ".join(
        table for party, rep, _, _ in PAIRS for table in (party, rep)
    ) + " IN SHARE ROW EXCLUSIVE MODE"))
    for party, rep, _, _ in PAIRS:
        invalid = connection.execute(sa.text(
            f"SELECT p.id, p.primary_signer_id FROM {party} p "
            f"LEFT JOIN {rep} r ON r.id=p.primary_signer_id AND r.parent_id=p.id "
            "WHERE p.primary_signer_id IS NOT NULL AND r.id IS NULL ORDER BY p.id LIMIT 10"
        )).all()
        if invalid:
            raise RuntimeError(
                f"AUD-02: orphan/mismatched primary signer in {party}: {invalid!r}; "
                "resolve existing links explicitly before retrying migration; no data was changed"
            )
    for party, rep, party_code, rep_code in PAIRS:
        op.create_unique_constraint(f"uq_{rep_code}_id_parent", rep, ["id", "parent_id"])
        op.create_foreign_key(
            f"fk_arm2_{party_code}_primary_signer_parent", party, rep,
            ["primary_signer_id", "id"], ["id", "parent_id"], ondelete="RESTRICT",
        )


def downgrade():
    for party, rep, party_code, rep_code in reversed(PAIRS):
        op.drop_constraint(f"fk_arm2_{party_code}_primary_signer_parent", party, type_="foreignkey")
        op.drop_constraint(f"uq_{rep_code}_id_parent", rep, type_="unique")
