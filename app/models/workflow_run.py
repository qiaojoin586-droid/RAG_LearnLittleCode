"""Durable, metadata-only checkpoints for a chat turn."""

import sqlalchemy as sa

from app.models.base import Base


workflow_runs = sa.Table(
    'workflow_runs', Base.metadata,
    sa.Column('id', sa.String(36), primary_key=True),
    sa.Column('turn_message_id', sa.Integer, sa.ForeignKey('chat_messages.id', ondelete='CASCADE'),
              nullable=False, unique=True),
    sa.Column('reply_message_id', sa.Integer, sa.ForeignKey('chat_messages.id', ondelete='SET NULL')),
    sa.Column('release_id', sa.String(36), sa.ForeignKey('ai_config_releases.id', ondelete='RESTRICT')),
    sa.Column('is_detached', sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column('execution_attempt', sa.Integer),
    sa.Column('job_payload', sa.JSON),
    sa.Column('job_deadline_at', sa.DateTime),
    sa.Column('plan_checkpoint', sa.JSON),
    sa.Column('status', sa.String(16), nullable=False),
    sa.Column('route', sa.String(16)),
    sa.Column('finished_steps', sa.JSON, nullable=False),
    sa.Column('state_version', sa.Integer, nullable=False),
    sa.Column('error_code', sa.String(64)),
    sa.Column('lease_expires_at', sa.DateTime, nullable=False),
    sa.Column('created_at', sa.DateTime, nullable=False),
    sa.Column('updated_at', sa.DateTime, nullable=False),
    sa.CheckConstraint("status IN ('running', 'succeeded', 'failed', 'cancelled')", name='ck_workflow_run_status'),
    sa.Index('ix_workflow_job_due', 'is_detached', 'status', 'lease_expires_at'),
)
