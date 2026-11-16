"""Link durable index catch-up jobs to the historical release they maintain."""

import re

from alembic import context, op
import sqlalchemy as sa


revision = 'c72d19e85a4b'
down_revision = 'ea03d94b72c1'
branch_labels = None
depends_on = None

TABLE = 'rag_index_builds'
COLUMN = 'restore_release_id'
INDEX = 'ix_rag_index_build_restore'
FOREIGN_KEY = 'fk_rag_index_build_restore'


def upgrade():
    offline = context.is_offline_mode()
    bind = None if offline else op.get_bind()
    inspector = sa.inspect(bind) if bind is not None else None
    columns = {item['name'] for item in inspector.get_columns(TABLE)} if inspector else set()
    if COLUMN not in columns:
        collation = None
        if bind is not None and bind.dialect.name == 'mysql':
            charset, collation = bind.execute(sa.text(
                "SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='ai_config_releases' AND COLUMN_NAME='id'"
            )).one()
            if charset != 'utf8mb4' or not re.fullmatch(r'utf8mb4_[a-z0-9_]+', collation or ''):
                raise RuntimeError('Historical release identifiers require a verified utf8mb4 collation')
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(36, collation=collation), nullable=True))

    inspector = sa.inspect(bind) if bind is not None else None
    indexes = {item['name'] for item in inspector.get_indexes(TABLE)} if inspector else set()
    foreign_keys = {item['name'] for item in inspector.get_foreign_keys(TABLE)} if inspector else set()
    if INDEX not in indexes:
        op.create_index(INDEX, TABLE, [COLUMN])
    if FOREIGN_KEY not in foreign_keys:
        op.create_foreign_key(FOREIGN_KEY, TABLE, 'ai_config_releases', [COLUMN], ['id'])


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
        f'SELECT 1 FROM {TABLE} WHERE {COLUMN} IS NOT NULL LIMIT 1'
    ).first():
        raise RuntimeError('Historical index catch-up jobs exist')
    op.drop_constraint(FOREIGN_KEY, TABLE, type_='foreignkey')
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
