"""Add ARM2 CP/ORG, representatives, and VEH operational owner."""

import sqlalchemy as sa

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def _party_table(name, prefix):
    op.create_table(
        name,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("full_name", sa.String(2000)), sa.Column("legal_address", sa.String(2000)),
        sa.Column("postal_address", sa.String(2000)), sa.Column("legal_form", sa.String(500)),
        sa.Column("short_name", sa.String(500)), sa.Column("inn", sa.String(12)),
        sa.Column("kpp", sa.String(9)), sa.Column("ogrn", sa.String(15)),
        sa.Column("phone", sa.String(200)), sa.Column("email", sa.String(320)),
        sa.Column("bank_name", sa.String(500)), sa.Column("bik", sa.String(9)),
        sa.Column("settlement_account", sa.String(20)), sa.Column("correspondent_account", sa.String(20)),
        sa.Column("primary_signer_id", sa.String()), sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(f"id ~ '^{prefix}-[0-9]{{6,}}$'", name=f"ck_{name}_id"),
        sa.CheckConstraint("name=btrim(name) AND name<>''", name=f"ck_{name}_name"),
        sa.CheckConstraint("status IN ('REVIEW','CONFIRMED')", name=f"ck_{name}_status"),
        sa.UniqueConstraint("inn", name=f"uq_{name}_inn"),
    )


def _representative_table(name, prefix, parent_table):
    op.create_table(
        name,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("parent_id", sa.String(), sa.ForeignKey(f"{parent_table}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False), sa.Column("position", sa.String(500)),
        sa.Column("authority_basis", sa.String(1000)), sa.Column("phone", sa.String(200)),
        sa.Column("email", sa.String(320)), sa.Column("state", sa.String(), nullable=False, server_default=sa.text("'Действующий'")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("last_name", sa.String(200)), sa.Column("first_name", sa.String(200)),
        sa.Column("middle_name", sa.String(200)),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(f"id ~ '^{prefix}-[0-9]{{6,}}$'", name=f"ck_{prefix.lower()}_id"),
        sa.CheckConstraint("state IN ('Действующий','Архивный')", name=f"ck_{prefix.lower()}_state"),
    )
    op.create_index(f"ix_{name}_parent", name, ["parent_id"])
    op.create_index(f"ix_{name}_user", name, ["user_id"])


def upgrade():
    for sequence in ("arm2_cp_id_seq", "arm2_org_id_seq", "arm2_cprep_id_seq", "arm2_orgrep_id_seq"):
        op.execute(sa.text(f"CREATE SEQUENCE {sequence}"))
    _party_table("arm2_counterparties", "CP")
    _party_table("arm2_organizations", "ORG")
    _representative_table("nsi_counterparty_representatives", "CPREP", "arm2_counterparties")
    _representative_table("nsi_organization_representatives", "ORGREP", "arm2_organizations")
    op.add_column("arm2_vehicles", sa.Column("owner_cp_id", sa.String(), nullable=True))
    op.add_column("arm2_vehicles", sa.Column("owner_org_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_arm2_vehicles_owner_cp", "arm2_vehicles", "arm2_counterparties", ["owner_cp_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_arm2_vehicles_owner_org", "arm2_vehicles", "arm2_organizations", ["owner_org_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_arm2_vehicles_one_owner", "arm2_vehicles", "NOT (owner_cp_id IS NOT NULL AND owner_org_id IS NOT NULL)")
    op.create_index("ix_arm2_vehicles_owner_cp", "arm2_vehicles", ["owner_cp_id"])
    op.create_index("ix_arm2_vehicles_owner_org", "arm2_vehicles", ["owner_org_id"])
    op.drop_constraint("ck_arm2_history_object_type", "arm2_history_events", type_="check")
    op.drop_constraint("ck_arm2_history_object_id", "arm2_history_events", type_="check")
    op.create_check_constraint("ck_arm2_history_object_type", "arm2_history_events", "object_type IN ('TYPE','VEH','CP','ORG','CPREP','ORGREP')")
    op.create_check_constraint("ck_arm2_history_object_id", "arm2_history_events", "(object_type='TYPE' AND object_id ~ '^TYPE-[0-9]{6,}$') OR (object_type='VEH' AND object_id ~ '^VEH-[0-9]{6,}$') OR (object_type='CP' AND object_id ~ '^CP-[0-9]{6,}$') OR (object_type='ORG' AND object_id ~ '^ORG-[0-9]{6,}$') OR (object_type='CPREP' AND object_id ~ '^CPREP-[0-9]{6,}$') OR (object_type='ORGREP' AND object_id ~ '^ORGREP-[0-9]{6,}$')")


def downgrade():
    op.drop_constraint("ck_arm2_history_object_id", "arm2_history_events", type_="check")
    op.drop_constraint("ck_arm2_history_object_type", "arm2_history_events", type_="check")
    op.create_check_constraint("ck_arm2_history_object_type", "arm2_history_events", "object_type IN ('TYPE','VEH')")
    op.create_check_constraint("ck_arm2_history_object_id", "arm2_history_events", "(object_type='TYPE' AND object_id ~ '^TYPE-[0-9]{6,}$') OR (object_type='VEH' AND object_id ~ '^VEH-[0-9]{6,}$')")
    op.drop_index("ix_arm2_vehicles_owner_org", table_name="arm2_vehicles")
    op.drop_index("ix_arm2_vehicles_owner_cp", table_name="arm2_vehicles")
    op.drop_constraint("ck_arm2_vehicles_one_owner", "arm2_vehicles", type_="check")
    op.drop_constraint("fk_arm2_vehicles_owner_org", "arm2_vehicles", type_="foreignkey")
    op.drop_constraint("fk_arm2_vehicles_owner_cp", "arm2_vehicles", type_="foreignkey")
    op.drop_column("arm2_vehicles", "owner_org_id")
    op.drop_column("arm2_vehicles", "owner_cp_id")
    op.drop_table("nsi_organization_representatives")
    op.drop_table("nsi_counterparty_representatives")
    op.drop_table("arm2_organizations")
    op.drop_table("arm2_counterparties")
    for sequence in ("arm2_orgrep_id_seq", "arm2_cprep_id_seq", "arm2_org_id_seq", "arm2_cp_id_seq"):
        op.execute(sa.text(f"DROP SEQUENCE {sequence}"))
