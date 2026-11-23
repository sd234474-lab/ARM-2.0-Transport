"""Enable one registered VEH calibration preset; no original, row or history rewriting."""
from sqlalchemy import text

from alembic import op

revision = '0073'
down_revision = '0072'
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint('ck_arm2_kind_file_naming', 'arm2_document_kinds', type_='check')
    op.create_check_constraint('ck_arm2_kind_file_naming', 'arm2_document_kinds',
        "file_naming_template IS NULL OR file_naming_template IN ('DOCUMENT_NAME','PARENT_AND_KIND','VEH_CALIBRATION')")
    op.create_check_constraint('ck_arm2_kind_calibration_naming', 'arm2_document_kinds',
        "file_naming_template IS DISTINCT FROM 'VEH_CALIBRATION' OR (lower(name)='тарировка' AND applicable_object_types='[\"VEH\"]'::jsonb)")


def downgrade():
    connection = op.get_bind()
    if connection.scalar(text("SELECT EXISTS(SELECT 1 FROM arm2_document_kinds WHERE file_naming_template='VEH_CALIBRATION')")) or connection.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM arm2_history_events WHERE object_type='DOC-KIND' AND
          (changes->'file_naming_template'->>'before'='VEH_CALIBRATION' OR
           changes->'file_naming_template'->>'after'='VEH_CALIBRATION'))""")):
        raise RuntimeError('0073 downgrade refused: calibration naming configuration/history exists; no data discarded')
    op.drop_constraint('ck_arm2_kind_calibration_naming', 'arm2_document_kinds', type_='check')
    op.drop_constraint('ck_arm2_kind_file_naming', 'arm2_document_kinds', type_='check')
    op.create_check_constraint('ck_arm2_kind_file_naming', 'arm2_document_kinds',
        "file_naming_template IS NULL OR file_naming_template IN ('DOCUMENT_NAME','PARENT_AND_KIND')")
