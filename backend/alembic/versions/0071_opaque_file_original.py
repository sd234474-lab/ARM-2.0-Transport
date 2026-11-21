"""Allow an explicitly classified opaque original; retain all existing FILE checks."""
from sqlalchemy import text

from alembic import op

revision = '0071'
down_revision = '0070'
branch_labels = depends_on = None

OLD = "extension IN ('pdf','png','jpg','gif','webp','docx','xlsx','pptx') AND stored_name<>'' AND original_upload_name<>'' AND storage_ref LIKE 'disk:/%'"
NEW = OLD.replace("'pptx')", "'pptx','bin')")


def upgrade():
    op.drop_constraint('ck_arm2_file_metadata', 'arm2_document_files', type_='check')
    op.create_check_constraint('ck_arm2_file_metadata', 'arm2_document_files', NEW)


def downgrade():
    if op.get_bind().scalar(text("SELECT EXISTS(SELECT 1 FROM arm2_document_files WHERE extension='bin')")):
        raise RuntimeError('0071 downgrade refused: opaque originals exist; no FILE or history is discarded')
    op.drop_constraint('ck_arm2_file_metadata', 'arm2_document_files', type_='check')
    op.create_check_constraint('ck_arm2_file_metadata', 'arm2_document_files', OLD)
