"""Persist the minimum inputs and ownership generation for read-only chat jobs."""

from alembic import context, op
import sqlalchemy as sa

revision = 'b891dc4fe605'
down_revision = 'a780cb3ed4f5'
branch_labels = None
depends_on = None

TABLE = 'workflow_runs'
INDEX = 'ix_workflow_job_due'
INDEX_COLUMNS = ['is_detached', 'status', 'lease_expires_at']
JOB_COLUMNS = (
    ('execution_attempt', sa.Integer),
    ('job_payload', sa.JSON),
    ('job_deadline_at', sa.DateTime),
)


def upgrade():
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    existing = {col['name']: col for col in inspector.get_columns(TABLE)} if inspector else {}
    indexes = {idx['name']: idx for idx in inspector.get_indexes(TABLE)} if inspector else {}
    for name, column_type in JOB_COLUMNS:
        column = existing.get(name)
        if column is None:
            op.add_column(TABLE, sa.Column(name, column_type, nullable=True))
        elif not column['nullable'] or column['default'] is not None or not isinstance(column['type'], column_type):
            raise RuntimeError(f'Existing workflow job column differs: {name}')
    index = indexes.get(INDEX)
    if index is None:
        op.create_index(INDEX, TABLE, INDEX_COLUMNS)
    elif index['column_names'] != INDEX_COLUMNS or index['unique']:
        raise RuntimeError('Existing workflow job index differs')


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        "SELECT 1 FROM workflow_runs WHERE execution_attempt IS NOT NULL AND status = 'running' LIMIT 1"
    ).first():
        raise RuntimeError('Pending chat jobs exist; drain them before downgrade')
    op.drop_index(INDEX, table_name=TABLE)
    for name, _ in reversed(JOB_COLUMNS):
        op.drop_column(TABLE, name)
