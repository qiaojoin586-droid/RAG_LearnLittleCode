"""Identify detached workflows for durable cancellation across processes."""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import TINYINT

revision = 'a780cb3ed4f5'
down_revision = 'f679ba2dc3e4'
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode():
        columns = {column['name']: column for column in sa.inspect(op.get_bind()).get_columns('workflow_runs')}
        existing = columns.get('is_detached')
        if existing is not None:
            if (existing['nullable']
                    or not isinstance(existing['type'], (sa.Boolean, TINYINT))
                    or str(existing['default']).strip("'()").lower() not in ('0', 'false')):
                raise RuntimeError('Existing workflow mode column differs')
            return
    op.add_column('workflow_runs', sa.Column('is_detached', sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        "SELECT 1 FROM workflow_runs WHERE is_detached = 1 AND status = 'running' AND lease_expires_at > CURRENT_TIMESTAMP LIMIT 1"
    ).first():
        raise RuntimeError('Active detached workflows exist; stop them before downgrade')
    op.drop_column('workflow_runs', 'is_detached')
