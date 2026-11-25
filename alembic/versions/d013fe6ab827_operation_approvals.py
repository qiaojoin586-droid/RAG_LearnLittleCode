"""Bind explicit user decisions to transactional tool receipts."""

from alembic import context, op
import sqlalchemy as sa

revision = 'd013fe6ab827'
down_revision = 'c902ed5fa716'
branch_labels = None
depends_on = None

TABLE = 'operation_approvals'


def upgrade():
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(TABLE):
            columns = {column['name']: column for column in inspector.get_columns(TABLE)}
            nonnull = {'operation_id', 'workflow_id', 'status', 'preview_hash', 'expires_at'}
            references = {(tuple(fk['constrained_columns']), fk['referred_table'], tuple(fk['referred_columns']),
                           fk['options'].get('ondelete')) for fk in inspector.get_foreign_keys(TABLE)}
            if (set(columns) != nonnull | {'arguments', 'target', 'decided_at'}
                    or any(columns[name]['nullable'] for name in nonnull)
                    or not all(isinstance(columns[name]['type'], sa.JSON) for name in ('arguments', 'target'))
                    or inspector.get_pk_constraint(TABLE)['constrained_columns'] != ['operation_id']
                    or references != {(('operation_id',), 'tool_operations', ('operation_id',), 'CASCADE'),
                                      (('workflow_id',), 'workflow_runs', ('id',), 'CASCADE')}):
                raise RuntimeError('Existing operation approval schema differs')
            if not any(index['name'] == 'ix_operation_approval_expiry' and index['column_names'] == ['status', 'expires_at']
                       for index in inspector.get_indexes(TABLE)):
                op.create_index('ix_operation_approval_expiry', TABLE, ['status', 'expires_at'])
            return
    op.create_table(
        TABLE,
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('tool_operations.operation_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('preview_hash', sa.String(64), nullable=False),
        sa.Column('arguments', sa.JSON(none_as_null=True)),
        sa.Column('target', sa.JSON(none_as_null=True)),
        sa.Column('expires_at', sa.DateTime, nullable=False),
        sa.Column('decided_at', sa.DateTime),
        sa.CheckConstraint("status IN ('pending', 'executed', 'rejected', 'expired', 'conflict')", name='ck_operation_approval_status'),
    )
    op.create_index('ix_operation_approval_expiry', TABLE, ['status', 'expires_at'])


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        'SELECT 1 FROM operation_approvals LIMIT 1'
    ).first():
        raise RuntimeError('Approval receipts exist; archive them before downgrade')
    op.drop_table(TABLE)
