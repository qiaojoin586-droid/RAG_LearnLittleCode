"""Carry provider model budgets across detached Worker attempts."""

from alembic import context, op
import sqlalchemy as sa

revision = 'f235b08cda49'
down_revision = 'e124af7bc938'
branch_labels = None
depends_on = None

BUDGETS = 'workflow_model_budgets'
RESERVATIONS = 'workflow_model_reservations'
RESERVATION_INDEX = 'ix_workflow_model_reservations_workflow_id'


def _has_table(name, required, primary_key, parent, parent_key):
    if context.is_offline_mode():
        return False
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(name):
        return False
    columns = {col['name']: col for col in inspector.get_columns(name)}
    if (set(columns) != required
            or inspector.get_pk_constraint(name)['constrained_columns'] != [primary_key]
            or not any(key['constrained_columns'] == ['workflow_id'] and key['referred_table'] == parent
                       and key['referred_columns'] == [parent_key] and key['options'].get('ondelete') == 'CASCADE'
                       for key in inspector.get_foreign_keys(name))):
        raise RuntimeError(f'Existing workflow model budget schema differs: {name}')
    return True


def upgrade():
    if not _has_table(BUDGETS, {'workflow_id', 'limits', 'calls', 'tokens', 'costs', 'stopped_reason'},
                      'workflow_id', 'workflow_runs', 'id'):
        op.create_table(
            BUDGETS,
            sa.Column('workflow_id', sa.String(36), sa.ForeignKey('workflow_runs.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('limits', sa.JSON, nullable=False),
            sa.Column('calls', sa.BigInteger, nullable=False),
            sa.Column('tokens', sa.BigInteger, nullable=False),
            sa.Column('costs', sa.JSON, nullable=False),
            sa.Column('stopped_reason', sa.String(64)),
        )
    if not _has_table(RESERVATIONS, {'id', 'workflow_id', 'execution_attempt', 'profile_revision_id', 'price_version_id',
                                     'input_bound', 'output_bound', 'currency', 'reserved_amount', 'status'},
                      'id', BUDGETS, 'workflow_id'):
        op.create_table(
            RESERVATIONS,
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('workflow_id', sa.String(36), sa.ForeignKey(f'{BUDGETS}.workflow_id', ondelete='CASCADE'), nullable=False),
            sa.Column('execution_attempt', sa.Integer, nullable=False),
            sa.Column('profile_revision_id', sa.String(36)),
            sa.Column('price_version_id', sa.String(36)),
            sa.Column('input_bound', sa.BigInteger, nullable=False),
            sa.Column('output_bound', sa.BigInteger, nullable=False),
            sa.Column('currency', sa.String(3)),
            sa.Column('reserved_amount', sa.Numeric(20, 10), nullable=False),
            sa.Column('status', sa.String(16), nullable=False),
            sa.CheckConstraint("status IN ('reserved', 'settled', 'over_bound')", name='ck_workflow_model_reservation_status'),
        )
    indexes = {} if context.is_offline_mode() else {
        item['name']: item['column_names'] for item in sa.inspect(op.get_bind()).get_indexes(RESERVATIONS)
    }
    if RESERVATION_INDEX not in indexes:
        op.create_index(RESERVATION_INDEX, RESERVATIONS, ['workflow_id'])
    elif indexes[RESERVATION_INDEX] != ['workflow_id']:
        raise RuntimeError('Existing workflow model reservation index differs')


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(f'SELECT 1 FROM {BUDGETS} LIMIT 1').first():
        raise RuntimeError('Workflow model budget receipts exist; archive them before downgrade')
    op.drop_table(RESERVATIONS)
    op.drop_table(BUDGETS)
