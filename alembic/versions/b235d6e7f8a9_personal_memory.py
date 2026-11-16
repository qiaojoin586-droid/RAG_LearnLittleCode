"""Personal memory tables, with collation matching verified existing identifiers."""
from pathlib import Path
import re

from alembic import context,op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision='b235d6e7f8a9'
down_revision='a124c5d6e7f8'
branch_labels=None
depends_on=None
NAMES=('user_memory_heads','user_memory_revisions','user_memory_jobs','user_memory_requests')
SOURCE_FIELDS=('last_source_request_id','last_source_message_id','last_source_completed_at')


def upgrade():
    source=(Path(__file__).resolve().parents[1]/'sql/user_memory_m1.sql').read_text(encoding='utf-8')
    statements=[part.strip() for part in source.split(';') if part.strip()]
    if len(statements)!=5:raise RuntimeError('Invalid memory migration resource')
    offline=context.is_offline_mode()
    bind=None if offline else op.get_bind()
    collation='utf8mb4_unicode_ci'
    if not offline:
        if bind.dialect.name!='mysql':raise RuntimeError('Memory migration requires MySQL 8')
        charset,collation=bind.execute(sa.text("SELECT CHARACTER_SET_NAME,COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='users' AND COLUMN_NAME='uuid'")).one()
        if charset!='utf8mb4' or not re.fullmatch(r'utf8mb4_[a-z0-9_]+',collation):raise RuntimeError('Unverified users.uuid collation')
    for statement in statements:
        if statement.startswith('CREATE TABLE'):
            name=re.match(r'CREATE TABLE (\w+)',statement)[1]
            if offline or not sa.inspect(bind).has_table(name):op.execute(statement.replace('COLLATE=utf8mb4_unicode_ci','COLLATE='+collation))
            else:
                from app.user_memory.tables import metadata
                expected=set(metadata.tables[name].columns.keys())
                actual={row['name'] for row in sa.inspect(bind).get_columns(name)}
                optional=set(SOURCE_FIELDS) if name=='user_memory_heads' else set()
                if expected-optional!=actual-optional:
                    raise RuntimeError('Existing memory table shape differs: '+name)
        elif offline or 'fk_um_head_revision' not in {row['name'] for row in sa.inspect(bind).get_foreign_keys('user_memory_heads')}:
            op.execute(statement)
    existing=set() if offline else {row['name'] for row in sa.inspect(bind).get_columns('user_memory_heads')}
    for column in (sa.Column('last_source_request_id',sa.CHAR(32),nullable=True),
                   sa.Column('last_source_message_id',sa.Integer(),nullable=True),
                   sa.Column('last_source_completed_at',mysql.DATETIME(fsp=6),nullable=True)):
        if column.name not in existing:op.add_column('user_memory_heads',column)
    if offline or 'memory_context' not in {row['name'] for row in sa.inspect(bind).get_columns('model_traces')}:
        op.add_column('model_traces',sa.Column('memory_context',sa.JSON(),nullable=True))


def downgrade():
    if not context.is_offline_mode():
        bind=op.get_bind()
        for name in NAMES:
            if sa.inspect(bind).has_table(name) and bind.exec_driver_sql(f'SELECT 1 FROM {name} LIMIT 1').first():
                raise RuntimeError('Personal memory exists; export/purge explicitly before schema downgrade')
        if bind.exec_driver_sql('SELECT 1 FROM model_traces WHERE memory_context IS NOT NULL LIMIT 1').first():
            raise RuntimeError('Personal memory trace provenance exists; retain schema or export explicitly')
    op.drop_constraint('fk_um_head_revision','user_memory_heads',type_='foreignkey')
    for name in reversed(NAMES):op.drop_table(name)
    op.drop_column('model_traces','memory_context')
