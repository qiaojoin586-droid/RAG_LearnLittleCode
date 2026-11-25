"""Persist knowledge uploads before dispatch and gate retrieval on publication."""

from alembic import context, op
import sqlalchemy as sa


revision = 'f64173ab9d20'
down_revision = 'e5312db748ce'
branch_labels = None
depends_on = None

DOCUMENTS = 'knowledge_documents'
JOBS = 'knowledge_ingest_jobs'


def upgrade():
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    columns = {item['name'] for item in inspector.get_columns(DOCUMENTS)} if inspector else set()
    if 'ready_for_retrieval' not in columns:
        op.add_column(DOCUMENTS, sa.Column('ready_for_retrieval', sa.Boolean(), nullable=False, server_default=sa.true()))
    if inspector is None or not inspector.has_table(JOBS):
        op.create_table(
            JOBS,
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('user_id', sa.String(36), sa.ForeignKey('users.uuid', ondelete='CASCADE'), nullable=False),
            sa.Column('document_id', sa.Integer, nullable=False, unique=True),
            sa.Column('release_id', sa.String(36), sa.ForeignKey('ai_config_releases.id', ondelete='RESTRICT')),
            sa.Column('filename', sa.String(500), nullable=False),
            sa.Column('file_path', sa.String(1000)),
            sa.Column('source_sha256', sa.String(64)),
            sa.Column('operation', sa.String(8), nullable=False),
            sa.Column('status', sa.String(12), nullable=False),
            sa.Column('stage', sa.String(16), nullable=False),
            sa.Column('attempts', sa.Integer, nullable=False),
            sa.Column('next_attempt_at', sa.DateTime, nullable=False),
            sa.Column('lease_token', sa.String(36)),
            sa.Column('lease_expires_at', sa.DateTime),
            sa.Column('error_code', sa.String(64)),
            sa.CheckConstraint("operation IN ('ingest', 'delete')", name='ck_knowledge_job_operation'),
            sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='ck_knowledge_job_status'),
        )
    indexes = {item['name'] for item in sa.inspect(op.get_bind()).get_indexes(JOBS)} if inspector else set()
    if 'ix_knowledge_job_due' not in indexes:
        op.create_index('ix_knowledge_job_due', JOBS, ['status', 'next_attempt_at'])
    if 'ix_knowledge_job_owner' not in indexes:
        op.create_index('ix_knowledge_job_owner', JOBS, ['user_id'])


def downgrade():
    if not context.is_offline_mode():
        bind = op.get_bind()
        if (bind.exec_driver_sql(f'SELECT 1 FROM {JOBS} LIMIT 1').first()
                or bind.exec_driver_sql(f'SELECT 1 FROM {DOCUMENTS} WHERE ready_for_retrieval = 0 LIMIT 1').first()):
            raise RuntimeError('Knowledge ingestion records exist; archive them before downgrade')
    op.drop_table(JOBS)
    op.drop_column(DOCUMENTS, 'ready_for_retrieval')
