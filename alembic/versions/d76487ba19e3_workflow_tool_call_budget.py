"""Persist attempted tool calls under the detached workflow's existing budget."""

from alembic import context, op
import sqlalchemy as sa


revision = 'd76487ba19e3'
down_revision = 'f235b08cda49'
branch_labels = None
depends_on = None

TABLE = 'workflow_model_budgets'


def upgrade():
    if not context.is_offline_mode():
        for column in sa.inspect(op.get_bind()).get_columns(TABLE):
            if column['name'] == 'tool_calls':
                if (column['nullable'] or not isinstance(column['type'], sa.BigInteger)
                        or str(column['default']).strip("()'\"") != '0'):
                    raise RuntimeError('Existing workflow tool counter differs')
                return
    op.add_column(TABLE, sa.Column('tool_calls', sa.BigInteger, nullable=False, server_default='0'))


def downgrade():
    if not context.is_offline_mode():
        table = sa.table(TABLE, sa.column('tool_calls', sa.BigInteger), sa.column('limits', sa.JSON))
        configured = sa.or_(table.c.tool_calls != 0, table.c.limits['max_tool_calls'].as_string().is_not(None))
        if op.get_bind().execute(sa.select(table.c.tool_calls).where(configured).limit(1)).first():
            raise RuntimeError('Workflow tool budgets exist; archive them before downgrade')
    op.drop_column(TABLE, 'tool_calls')
