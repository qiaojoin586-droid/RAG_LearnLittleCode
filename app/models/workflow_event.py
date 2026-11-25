"""Ordered, replay-safe chat progress stored separately from messages."""

import sqlalchemy as sa

from app.models.base import Base


workflow_events = sa.Table(
    'workflow_events', Base.metadata,
    sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), primary_key=True),
    sa.Column('sequence', sa.Integer, primary_key=True),
    sa.Column('payload', sa.JSON, nullable=False),
)
