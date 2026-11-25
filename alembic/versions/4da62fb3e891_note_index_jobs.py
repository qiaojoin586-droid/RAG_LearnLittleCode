"""Persist note index work in the transaction that changes the source."""

from alembic import context, op
import sqlalchemy as sa


revision = '4da62fb3e891'
down_revision = 'f64173ab9d20'
branch_labels = None
depends_on = None

TABLE = 'note_index_jobs'
INDEXES = {
    'ix_note_index_due': ['status', 'next_attempt_at'],
    'ix_note_index_lease': ['status', 'lease_expires_at'],
    'ix_note_index_owner': ['user_id'],
}


def upgrade():
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    if inspector is None or not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
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
            sa.CheckConstraint("operation IN ('upsert', 'delete')", name='ck_note_index_operation'),
            sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='ck_note_index_status'),
        )
    indexes = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(TABLE)} if inspector else set()
    for name, columns in INDEXES.items():
        if name not in indexes:
            op.create_index(name, TABLE, columns)


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        f'SELECT 1 FROM {TABLE} LIMIT 1'
    ).first():
        raise RuntimeError('Note index jobs exist; reconcile and archive them before downgrade')
    op.drop_table(TABLE)
