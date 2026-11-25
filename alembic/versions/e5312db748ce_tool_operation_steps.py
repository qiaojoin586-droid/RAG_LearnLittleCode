"""Associate recoverable tool receipts with workflow steps."""

from alembic import context, op
import sqlalchemy as sa


revision = 'e5312db748ce'
down_revision = 'd76487ba19e3'
branch_labels = None
depends_on = None
TABLE = 'tool_operation_steps'


def upgrade():
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(TABLE):
            if (inspector.get_pk_constraint(TABLE)['constrained_columns'] != ['workflow_id', 'step_number']
                    or {column['name'] for column in inspector.get_columns(TABLE)} != {'workflow_id', 'step_number', 'operation_id'}):
                raise RuntimeError('Existing tool operation step schema differs')
            return
    op.create_table(
        TABLE,
        sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('step_number', sa.Integer, primary_key=True),
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('tool_operations.operation_id', ondelete='CASCADE'), nullable=False),
        sa.CheckConstraint('step_number >= 0', name='ck_tool_operation_step_number'),
    )


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(f'SELECT 1 FROM {TABLE} LIMIT 1').first():
        raise RuntimeError('Tool operation step receipts exist; archive them before downgrade')
    op.drop_table(TABLE)
