"""Add a private, bounded plan checkpoint for durable chat."""

from alembic import context, op
import sqlalchemy as sa

revision = 'c902ed5fa716'
down_revision = 'b891dc4fe605'
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode():
        columns = sa.inspect(op.get_bind()).get_columns('workflow_runs')
        for column in columns:
            if column['name'] == 'plan_checkpoint':
                if not column['nullable'] or column['default'] is not None or not isinstance(column['type'], sa.JSON):
                    raise RuntimeError('Existing plan checkpoint column differs')
                return
    op.add_column('workflow_runs', sa.Column('plan_checkpoint', sa.JSON, nullable=True))


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        "SELECT 1 FROM workflow_runs WHERE status = 'running' AND plan_checkpoint IS NOT NULL LIMIT 1"
    ).first():
        raise RuntimeError('Active plan checkpoints exist; drain them before downgrade')
    op.drop_column('workflow_runs', 'plan_checkpoint')
