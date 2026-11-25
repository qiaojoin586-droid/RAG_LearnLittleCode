"""Durable receipts for writes committed in the same database transaction."""

import sqlalchemy as sa

from app.models.base import Base


OPERATION_SCOPE = ('turn_message_id', 'user_id', 'tool_name', 'arguments_hash')
tool_operations = sa.Table(
    'tool_operations', Base.metadata,
    sa.Column('operation_id', sa.String(36), primary_key=True),
    sa.Column('user_id', sa.String(36), nullable=False),
    sa.Column('turn_message_id', sa.Integer, sa.ForeignKey('chat_messages.id', ondelete='CASCADE'), nullable=False),
    sa.Column('tool_name', sa.String(50), nullable=False),
    sa.Column('arguments_hash', sa.String(64), nullable=False),
    sa.Column('release_id', sa.String(36)),
    sa.Column('result_ref', sa.JSON(none_as_null=True)),
    sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint(*OPERATION_SCOPE, name='uq_tool_operation_scope'),
)

tool_operation_steps = sa.Table(
    'tool_operation_steps', Base.metadata,
    sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), primary_key=True),
    sa.Column('step_number', sa.Integer, primary_key=True),
    sa.Column('operation_id', sa.String(36), sa.ForeignKey('tool_operations.operation_id', ondelete='CASCADE'), nullable=False),
    sa.CheckConstraint('step_number >= 0', name='ck_tool_operation_step_number'),
)
