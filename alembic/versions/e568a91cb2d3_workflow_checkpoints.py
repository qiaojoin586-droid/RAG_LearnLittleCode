"""Persist metadata-only chat workflow checkpoints."""

from alembic import context, op
import sqlalchemy as sa

revision = 'e568a91cb2d3'
down_revision = 'd457f809b1c2'
branch_labels = None
depends_on = None
TABLE = 'workflow_runs'


def upgrade():
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(TABLE):
            verify_existing(inspector)
            return
    op.create_table(
        TABLE,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('turn_message_id', sa.Integer, sa.ForeignKey('chat_messages.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('reply_message_id', sa.Integer, sa.ForeignKey('chat_messages.id', ondelete='SET NULL')),
        sa.Column('release_id', sa.String(36), sa.ForeignKey('ai_config_releases.id', ondelete='RESTRICT')),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('route', sa.String(16)),
        sa.Column('finished_steps', sa.JSON, nullable=False),
        sa.Column('state_version', sa.Integer, nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('lease_expires_at', sa.DateTime, nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=False),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed', 'cancelled')", name='ck_workflow_run_status'),
    )


def verify_existing(inspector):
    columns = {column['name']: column for column in inspector.get_columns(TABLE)}
    required = {'id', 'turn_message_id', 'status', 'finished_steps', 'state_version', 'lease_expires_at',
                'created_at', 'updated_at'}
    foreign_keys = {(tuple(key['constrained_columns']), key['referred_table'], tuple(key['referred_columns']))
                    for key in inspector.get_foreign_keys(TABLE)}
    expected_keys = {(('turn_message_id',), 'chat_messages', ('id',)),
                     (('reply_message_id',), 'chat_messages', ('id',)),
                     (('release_id',), 'ai_config_releases', ('id',))}
    if (set(columns) != required | {'reply_message_id', 'release_id', 'route', 'error_code'}
            or any(columns[name]['nullable'] for name in required)
            or not isinstance(columns['finished_steps']['type'], sa.JSON)
            or inspector.get_pk_constraint(TABLE)['constrained_columns'] != ['id']
            or not any(key['column_names'] == ['turn_message_id'] for key in inspector.get_unique_constraints(TABLE))
            or foreign_keys != expected_keys):
        raise RuntimeError('Existing workflow checkpoint schema differs')


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        'SELECT 1 FROM workflow_runs LIMIT 1'
    ).first():
        raise RuntimeError('Workflow checkpoints exist; archive them before downgrade')
    op.drop_table(TABLE)
