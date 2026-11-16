"""Add durable receipts for transactional Agent tool writes."""

from alembic import context, op
import sqlalchemy as sa

revision = 'd457f809b1c2'
down_revision = 'c346e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table('tool_operations'):
            verify_existing(inspector)
            return
    op.create_table(
        'tool_operations',
        sa.Column('operation_id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('turn_message_id', sa.Integer, sa.ForeignKey('chat_messages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tool_name', sa.String(50), nullable=False),
        sa.Column('arguments_hash', sa.String(64), nullable=False),
        sa.Column('release_id', sa.String(36)),
        sa.Column('result_ref', sa.JSON(none_as_null=True)),
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('turn_message_id', 'user_id', 'tool_name', 'arguments_hash', name='uq_tool_operation_scope'),
    )


def verify_existing(inspector):
    table = 'tool_operations'
    columns = {col['name']: col for col in inspector.get_columns(table)}
    required = {'operation_id', 'user_id', 'turn_message_id', 'tool_name', 'arguments_hash', 'created_at'}
    scope = ['turn_message_id', 'user_id', 'tool_name', 'arguments_hash']
    if (set(columns) != required | {'release_id', 'result_ref'}
            or any(columns[name]['nullable'] for name in required)
            or not isinstance(columns['result_ref']['type'], sa.JSON)
            or inspector.get_pk_constraint(table)['constrained_columns'] != ['operation_id']
            or not any(key['column_names'] == scope for key in inspector.get_unique_constraints(table))
            or not any(key['constrained_columns'] == ['turn_message_id'] and key['referred_table'] == 'chat_messages'
                       and key['referred_columns'] == ['id'] and key['options'].get('ondelete') == 'CASCADE'
                       for key in inspector.get_foreign_keys(table))):
        raise RuntimeError('Existing tool receipt schema differs')


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        'SELECT 1 FROM tool_operations LIMIT 1'
    ).first():
        raise RuntimeError('Tool receipts exist; archive them before downgrade')
    op.drop_table('tool_operations')
