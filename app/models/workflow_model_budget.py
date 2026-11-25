"""Persistent limits and provider attempt reservations for a detached chat."""

import sqlalchemy as sa

from app.models.base import Base


workflow_model_budgets = sa.Table(
    'workflow_model_budgets', Base.metadata,
    sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), primary_key=True),
    sa.Column('limits', sa.JSON, nullable=False),
    sa.Column('calls', sa.BigInteger, nullable=False),
    sa.Column('tool_calls', sa.BigInteger, nullable=False, server_default='0'),
    sa.Column('tokens', sa.BigInteger, nullable=False),
    sa.Column('costs', sa.JSON, nullable=False),
    sa.Column('stopped_reason', sa.String(64)),
)

workflow_model_reservations = sa.Table(
    'workflow_model_reservations', Base.metadata,
    sa.Column('id', sa.String(36), primary_key=True),
    sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_model_budgets.workflow_id', ondelete='CASCADE'),
              nullable=False, index=True),
    sa.Column('execution_attempt', sa.Integer, nullable=False),
    sa.Column('profile_revision_id', sa.String(36)),
    sa.Column('price_version_id', sa.String(36)),
    sa.Column('input_bound', sa.BigInteger, nullable=False),
    sa.Column('output_bound', sa.BigInteger, nullable=False),
    sa.Column('currency', sa.String(3)),
    sa.Column('reserved_amount', sa.Numeric(20, 10), nullable=False),
    sa.Column('status', sa.String(16), nullable=False),
    sa.CheckConstraint("status IN ('reserved', 'settled', 'over_bound')", name='ck_workflow_model_reservation_status'),
)
