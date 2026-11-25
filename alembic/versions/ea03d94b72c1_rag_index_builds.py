"""Persist candidate index builds and their write leases."""

from alembic import context, op
import re
import sqlalchemy as sa


revision = 'ea03d94b72c1'
down_revision = '4da62fb3e891'
branch_labels = None
depends_on = None

TABLE = 'rag_index_builds'


def upgrade():
    offline = context.is_offline_mode()
    if offline or not sa.inspect(op.get_bind()).has_table(TABLE):
        collation = 'utf8mb4_unicode_ci'
        if not offline and op.get_bind().dialect.name == 'mysql':
            charset, collation = op.get_bind().execute(sa.text(
                "SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='ai_config_drafts' AND COLUMN_NAME='id'"
            )).one()
            if charset != 'utf8mb4' or not re.fullmatch(r'utf8mb4_[a-z0-9_]+', collation or ''):
                raise RuntimeError('Candidate build foreign keys require verified draft identifier collation')
        op.create_table(
            TABLE,
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('draft_id', sa.String(36), sa.ForeignKey('ai_config_drafts.id'), nullable=False),
            sa.Column('draft_version', sa.Integer, nullable=False),
            sa.Column('actor_id', sa.String(36), nullable=False),
            sa.Column('policy_version', sa.BigInteger, nullable=False),
            sa.Column('target_hash', sa.String(64), nullable=False),
            sa.Column('status', sa.String(24), nullable=False),
            sa.Column('next_run_at', sa.DateTime, nullable=False),
            sa.Column('lease_token', sa.String(36)),
            sa.Column('lease_expires_at', sa.DateTime),
            sa.Column('attempts', sa.Integer, nullable=False),
            sa.Column('collections', sa.JSON),
            sa.Column('index_ids', sa.JSON),
            sa.Column('source_watermark', sa.String(64)),
            sa.Column('error_code', sa.String(64)),
            sa.Column('created_at', sa.DateTime, nullable=False),
            mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate=collation,
        )
    names = set() if offline else {item['name'] for item in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if 'ix_rag_index_build_due' not in names:
        op.create_index('ix_rag_index_build_due', TABLE, ['status', 'next_run_at'])
    if 'ix_rag_index_build_draft' not in names:
        op.create_index('ix_rag_index_build_draft', TABLE, ['draft_id'])


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        f'SELECT 1 FROM {TABLE} LIMIT 1'
    ).first():
        raise RuntimeError('Candidate index builds exist')
    op.drop_table(TABLE)
