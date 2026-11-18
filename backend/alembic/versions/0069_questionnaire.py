"""B5 private questionnaire workflow and normalized CP personal names."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = '0069'
down_revision = '0068'
branch_labels = depends_on = None


def upgrade():
    for field in ('last_name', 'first_name', 'middle_name'):
        op.add_column('arm2_counterparties', sa.Column(field, sa.String(200), nullable=True))
    op.create_table('arm2_questionnaire_drafts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('author_id', sa.Integer, sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False, server_default='1'),
        sa.Column('status', sa.String, nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('filled_date', sa.Date),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('accepted_at', sa.DateTime(timezone=True)),
        sa.Column('payload', JSONB, nullable=False), sa.Column('choices', JSONB, nullable=False),
        sa.Column('preview', JSONB, nullable=False), sa.Column('staging', JSONB, nullable=False),
        sa.CheckConstraint("status IN ('OPEN','ACCEPTED','CANCELLED') AND revision>0", name='ck_questionnaire_state'))
    op.create_index('ix_questionnaire_author', 'arm2_questionnaire_drafts', ['author_id'])
    op.create_table('arm2_questionnaire_uploads',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('draft_id', sa.String(36), sa.ForeignKey('arm2_questionnaire_drafts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False), sa.Column('size', sa.Integer, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False), sa.Column('media_type', sa.String),
        sa.Column('extension', sa.String), sa.Column('error', sa.String),
        sa.Column('state', sa.String, nullable=False), sa.Column('prepared', JSONB, nullable=False),
        sa.CheckConstraint("id ~ '^[0-9a-f]{64}$' AND size>=0 AND state IN ('STAGED','UPLOADED','ACCEPTED','DISCARDED')", name='ck_questionnaire_upload'))
    op.create_table('arm2_questionnaire_receipts',
        sa.Column('command_id', sa.String(36), primary_key=True),
        sa.Column('draft_id', sa.String(36), sa.ForeignKey('arm2_questionnaire_drafts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('author_id', sa.Integer, sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('result', JSONB, nullable=False))
    op.create_table('arm2_questionnaire_exports',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('author_id', sa.Integer, sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('cp_id', sa.String, sa.ForeignKey('arm2_counterparties.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('incomplete_reason', sa.String(500)), sa.Column('missing', JSONB, nullable=False))


def downgrade():
    db = op.get_bind()
    tables = ['arm2_questionnaire_exports', 'arm2_questionnaire_receipts', 'arm2_questionnaire_uploads', 'arm2_questionnaire_drafts']
    db.execute(sa.text('LOCK TABLE ' + ','.join([*tables, 'arm2_counterparties', 'arm2_history_events']) + ' IN ACCESS EXCLUSIVE MODE'))
    if (any(db.scalar(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table})')) for table in tables)
        or db.scalar(sa.text('''SELECT EXISTS(SELECT 1 FROM arm2_counterparties WHERE last_name IS NOT NULL OR first_name IS NOT NULL OR middle_name IS NOT NULL)
            OR EXISTS(SELECT 1 FROM arm2_history_events WHERE object_type='CP' AND EXISTS(
                SELECT 1 FROM jsonb_each(changes) c WHERE c.key IN ('last_name','first_name','middle_name')
                AND ((c.value->>'after') IS NOT NULL OR (c.value->>'before') IS NOT NULL)))'''))):
        raise RuntimeError('B5 data/history exists; downgrade refused')
    for table in tables:
        op.drop_table(table)
    for field in ('middle_name', 'first_name', 'last_name'):
        op.drop_column('arm2_counterparties', field)
