"""Persist the dispatch boundary separately from the chat content."""

from alembic import context, op
import sqlalchemy as sa

revision = 'e124af7bc938'
down_revision = 'd013fe6ab827'
branch_labels = None
depends_on = None

TABLE = 'email_dispatches'
INDEXES = {
    'ix_email_dispatch_user_started': ['user_id', 'started_at'],
    'ix_email_dispatch_status_deadline': ['status', 'deadline_at'],
}


def upgrade():
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    existing_indexes = {}
    if inspector is not None and inspector.has_table(TABLE):
        columns = {column['name']: column for column in inspector.get_columns(TABLE)}
        if (set(columns) != {'operation_id', 'user_id', 'status', 'started_at', 'deadline_at', 'updated_at'}
                or any(column['nullable'] for column in columns.values())
                or inspector.get_pk_constraint(TABLE)['constrained_columns'] != ['operation_id']
                or not any(key['constrained_columns'] == ['user_id'] and key['referred_table'] == 'users'
                           and key['referred_columns'] == ['uuid'] and key['options'].get('ondelete') == 'CASCADE'
                           for key in inspector.get_foreign_keys(TABLE))):
            raise RuntimeError('Existing email dispatch schema differs')
        existing_indexes = {index['name']: index['column_names'] for index in inspector.get_indexes(TABLE)}
    else:
        op.create_table(
            TABLE,
            sa.Column('operation_id', sa.String(36), primary_key=True),
            sa.Column('user_id', sa.String(36), sa.ForeignKey('users.uuid', ondelete='CASCADE'), nullable=False),
            sa.Column('status', sa.String(16), nullable=False),
            sa.Column('started_at', sa.DateTime, nullable=False),
            sa.Column('deadline_at', sa.DateTime, nullable=False),
            sa.Column('updated_at', sa.DateTime, nullable=False),
            sa.CheckConstraint("status IN ('dispatching', 'accepted', 'unknown', 'failed', 'received')", name='ck_email_dispatch_status'),
        )
    for name, columns in INDEXES.items():
        if name not in existing_indexes:
            op.create_index(name, TABLE, columns)
        elif existing_indexes[name] != columns:
            raise RuntimeError('Existing email dispatch index differs')


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql('SELECT 1 FROM email_dispatches LIMIT 1').first():
        raise RuntimeError('Email dispatch receipts exist; archive them before downgrade')
    op.drop_table(TABLE)
