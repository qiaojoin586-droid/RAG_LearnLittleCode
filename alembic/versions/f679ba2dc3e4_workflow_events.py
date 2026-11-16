"""Add a bounded, replay-safe chat event journal."""

from alembic import context, op
import sqlalchemy as sa

revision = 'f679ba2dc3e4'
down_revision = 'e568a91cb2d3'
branch_labels = None
depends_on = None
TABLE = 'workflow_events'


def upgrade():
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(TABLE):
            columns = {column['name']: column for column in inspector.get_columns(TABLE)}
            if (set(columns) != {'workflow_id', 'sequence', 'payload'}
                    or any(column['nullable'] for column in columns.values())
                    or not isinstance(columns['payload']['type'], sa.JSON)
                    or inspector.get_pk_constraint(TABLE)['constrained_columns'] != ['workflow_id', 'sequence']
                    or not any(key['constrained_columns'] == ['workflow_id'] and key['referred_table'] == 'workflow_runs'
                               and key['referred_columns'] == ['id'] and key['options'].get('ondelete') == 'CASCADE'
                               for key in inspector.get_foreign_keys(TABLE))):
                raise RuntimeError('Existing workflow event schema differs')
            return
    op.create_table(
        TABLE,
        sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('sequence', sa.Integer, primary_key=True),
        sa.Column('payload', sa.JSON, nullable=False),
    )


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        'SELECT 1 FROM workflow_events LIMIT 1'
    ).first():
        raise RuntimeError('Workflow events exist; archive them before downgrade')
    op.drop_table(TABLE)
