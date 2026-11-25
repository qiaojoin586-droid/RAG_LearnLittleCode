"""Durable upload and cleanup jobs for knowledge documents."""

import sqlalchemy as sa

from app.models.base import Base


knowledge_ingest_jobs = sa.Table(
    'knowledge_ingest_jobs', Base.metadata,
    sa.Column('id', sa.String(36), primary_key=True),
    sa.Column('user_id', sa.String(36), sa.ForeignKey('users.uuid', ondelete='CASCADE'), nullable=False),
    # Keep the original document ID after its row is deleted, for vector cleanup.
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
    sa.Index('ix_knowledge_job_due', 'status', 'next_attempt_at'),
    sa.Index('ix_knowledge_job_owner', 'user_id'),
)
