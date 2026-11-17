"""TYPE category; audited completeness transition without inferred categories."""
import sqlalchemy as sa

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = depends_on = None


def upgrade():
    op.add_column("arm2_vehicle_types", sa.Column("plate_category", sa.String(), nullable=True))
    op.create_check_constraint("ck_arm2_type_plate_category", "arm2_vehicle_types",
                              "plate_category IS NULL OR plate_category IN ('Автомобильный', 'Самоходная техника')")
    op.execute("""
        INSERT INTO arm2_history_events
        (object_type, object_id, operation, actor_kind, actor_id, source, correlation_id,
         version_before, version_after, changes, related_objects)
        SELECT 'TYPE', id, 'UPDATED', 'SYSTEM', 'migration-0065', 'arm2-migration', 'migration-0065',
               version, version+1,
               jsonb_build_object('status', jsonb_build_object('before',status,'after','REVIEW'),
                                  'version', jsonb_build_object('before',version,'after',version+1)), '[]'::jsonb
        FROM arm2_vehicle_types WHERE status <> 'REVIEW'
    """)
    op.execute("UPDATE arm2_vehicle_types SET status='REVIEW', version=version+1 WHERE status <> 'REVIEW'")


def downgrade():
    db = op.get_bind()
    db.execute(sa.text("LOCK TABLE arm2_vehicle_types, arm2_history_events IN ACCESS EXCLUSIVE MODE"))
    if db.scalar(sa.text("""SELECT EXISTS(SELECT 1 FROM arm2_vehicle_types WHERE plate_category IS NOT NULL)
        OR EXISTS(SELECT 1 FROM arm2_history_events WHERE object_type='TYPE' AND changes ? 'plate_category')""")):
        raise RuntimeError("TYPE category data/history exists; downgrade refused")
    op.drop_constraint("ck_arm2_type_plate_category", "arm2_vehicle_types", type_="check")
    op.drop_column("arm2_vehicle_types", "plate_category")
