"""Explicit CP type; CP/ORG have no description completeness status."""
import sqlalchemy as sa

from alembic import op

revision = '0070'
down_revision = '0069'
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS(SELECT 1 FROM arm2_counterparties)')):
        raise RuntimeError('0070 requires an empty CP catalogue. Use the separately authorized test-only reset; migration never deletes business data or guesses CP type.')
    op.add_column('arm2_counterparties', sa.Column('type', sa.String(30), nullable=False))
    op.create_check_constraint('ck_arm2_counterparties_type', 'arm2_counterparties', "type IN ('Физическое лицо','Юридическое лицо')")
    op.create_check_constraint('ck_arm2_counterparties_legal_form', 'arm2_counterparties', "type<>'Физическое лицо' OR legal_form IS NULL OR legal_form='ИП'")
    for table in ('arm2_counterparties', 'arm2_organizations'):
        op.drop_constraint('ck_' + table + '_status', table, type_='check')
        op.drop_column(table, 'status')
    op.create_index('uq_arm2_questionnaire_active_content', 'arm2_questionnaire_drafts',
                    ['author_id', 'source_sha256'], unique=True, postgresql_where=sa.text("status='OPEN'"))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS(SELECT 1 FROM arm2_counterparties) OR EXISTS(SELECT 1 FROM arm2_organizations)')):
        raise RuntimeError('0070 CP/ORG data present; downgrade refused. Never discard explicit CP types or guess completeness.')
    op.drop_index('uq_arm2_questionnaire_active_content', table_name='arm2_questionnaire_drafts')
    for table in ('arm2_counterparties', 'arm2_organizations'):
        op.add_column(table, sa.Column('status', sa.String, nullable=False, server_default='REVIEW'))
        op.alter_column(table, 'status', server_default=None)
        op.create_check_constraint('ck_' + table + '_status', table, "status IN ('REVIEW','CONFIRMED')")
    op.drop_constraint('ck_arm2_counterparties_legal_form', 'arm2_counterparties', type_='check')
    op.drop_constraint('ck_arm2_counterparties_type', 'arm2_counterparties', type_='check')
    op.drop_column('arm2_counterparties', 'type')
