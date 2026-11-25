"""One durable SMTP dispatch reservation per confirmed chat operation."""

import sqlalchemy as sa

from app.models.base import Base


email_dispatches = sa.Table(
    'email_dispatches', Base.metadata,
    # The reservation survives chat deletion until its rate-limit window has passed.
    sa.Column('operation_id', sa.String(36), primary_key=True),
    sa.Column('user_id', sa.String(36), sa.ForeignKey('users.uuid', ondelete='CASCADE'), nullable=False),
    sa.Column('status', sa.String(16), nullable=False),
    sa.Column('started_at', sa.DateTime, nullable=False),
    sa.Column('deadline_at', sa.DateTime, nullable=False),
    sa.Column('updated_at', sa.DateTime, nullable=False),
    sa.Index('ix_email_dispatch_user_started', 'user_id', 'started_at'),
    sa.Index('ix_email_dispatch_status_deadline', 'status', 'deadline_at'),
    sa.CheckConstraint("status IN ('dispatching', 'accepted', 'unknown', 'failed', 'received')", name='ck_email_dispatch_status'),
)
