"""Latest desired vector index state for each note."""

import sqlalchemy as sa

from app.models.base import Base


note_index_jobs = sa.Table(
    'note_index_jobs', Base.metadata,
    # Preserve cleanup jobs after the source note is permanently deleted.
    sa.Column('note_id', sa.String(36), primary_key=True),
    sa.Column('user_id', sa.String(36), sa.ForeignKey('users.uuid', ondelete='CASCADE'), nullable=False),
    sa.Column('revision', sa.String(36), nullable=False),
    sa.Column('operation', sa.String(8), nullable=False),
    sa.Column('status', sa.String(12), nullable=False),
    sa.Column('attempts', sa.Integer, nullable=False),
    sa.Column('next_attempt_at', sa.DateTime, nullable=False),
    sa.Column('lease_token', sa.String(36)),
    sa.Column('lease_expires_at', sa.DateTime),
    sa.Column('error_code', sa.String(64)),
    sa.Column('source_manifest', sa.JSON(none_as_null=True)),
    sa.CheckConstraint("operation IN ('upsert', 'delete')", name='ck_note_index_operation'),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='ck_note_index_status'),
    sa.Index('ix_note_index_due', 'status', 'next_attempt_at'),
    sa.Index('ix_note_index_lease', 'status', 'lease_expires_at'),
    sa.Index('ix_note_index_owner', 'user_id'),
)


note_search_chunks = sa.Table(
    'note_search_chunks', Base.metadata,
    sa.Column('note_id', sa.String(36), sa.ForeignKey('notes.id', ondelete='CASCADE'), primary_key=True),
    sa.Column('ordinal', sa.Integer, primary_key=True),
    sa.Column('revision', sa.String(36), nullable=False),
    sa.Column('title', sa.Text, nullable=False),
    sa.Column('content', sa.Text, nullable=False),
    sa.Index('ft_note_chunks_text', 'title', 'content', mysql_prefix='FULLTEXT',
             mysql_with_parser='ngram').ddl_if(dialect='mysql'),
    mysql_engine='InnoDB',
)
