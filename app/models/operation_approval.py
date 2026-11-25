"""User decisions for proposed transactional chat operations."""

import sqlalchemy as sa

from app.models.base import Base


operation_approvals = sa.Table(
    'operation_approvals', Base.metadata,
    sa.Column('operation_id', sa.String(36), sa.ForeignKey('tool_operations.operation_id', ondelete='CASCADE'),
              primary_key=True),
    sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), nullable=False),
    sa.Column('status', sa.String(16), nullable=False),
    sa.Column('preview_hash', sa.String(64), nullable=False),
    sa.Column('arguments', sa.JSON(none_as_null=True)),
    sa.Column('target', sa.JSON(none_as_null=True)),
    sa.Column('expires_at', sa.DateTime, nullable=False),
    sa.Column('decided_at', sa.DateTime),
    sa.Index('ix_operation_approval_expiry', 'status', 'expires_at'),
    sa.CheckConstraint("status IN ('pending', 'executed', 'rejected', 'expired', 'conflict')", name='ck_operation_approval_status'),
)
