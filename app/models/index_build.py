"""Durable work for a candidate RAG index pair."""

import sqlalchemy as sa

from app.models.base import Base


index_builds = sa.Table(
    'rag_index_builds', Base.metadata,
    sa.Column('id', sa.String(36), primary_key=True),
    sa.Column('draft_id', sa.String(36), sa.ForeignKey('ai_config_drafts.id'), nullable=False),
    sa.Column('draft_version', sa.Integer, nullable=False),
    sa.Column('restore_release_id', sa.String(36), sa.ForeignKey('ai_config_releases.id', name='fk_rag_index_build_restore')),
    sa.Column('actor_id', sa.String(36), nullable=False),
    sa.Column('policy_version', sa.BigInteger, nullable=False),
    sa.Column('target_hash', sa.String(64), nullable=False),
    sa.Column('status', sa.String(24), nullable=False),
    sa.Column('next_run_at', sa.DateTime, nullable=False),
    sa.Column('lease_token', sa.String(36)),
    sa.Column('lease_expires_at', sa.DateTime),
    sa.Column('attempts', sa.Integer, nullable=False),
    sa.Column('collections', sa.JSON),
    sa.Column('index_ids', sa.JSON),
    sa.Column('source_watermark', sa.String(64)),
    sa.Column('error_code', sa.String(64)),
    sa.Column('created_at', sa.DateTime, nullable=False),
    sa.Index('ix_rag_index_build_due', 'status', 'next_run_at'),
    sa.Index('ix_rag_index_build_draft', 'draft_id'),
    sa.Index('ix_rag_index_build_restore', 'restore_release_id'),
    mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci',
)
