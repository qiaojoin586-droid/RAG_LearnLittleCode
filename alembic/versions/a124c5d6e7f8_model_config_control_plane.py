"""M1 configuration tables and nullable trace provenance.

Revision ID: a124c5d6e7f8
Revises: f1c2d3e4a5b6
"""
from pathlib import Path
import re

from alembic import context, op
import sqlalchemy as sa

revision = 'a124c5d6e7f8'
down_revision = 'f1c2d3e4a5b6'
branch_labels = None
depends_on = None

LATE_FOREIGN_KEYS = (
    ('ai_secrets','origin_draft_id','ai_config_drafts','fk_secret_origin_draft'),
    ('ai_profile_revisions','source_draft_id','ai_config_drafts','fk_profile_source_draft'),
    ('ai_prompt_revisions','source_draft_id','ai_config_drafts','fk_prompt_source_draft'),
    ('ai_config_validations','request_record_id','ai_config_requests','fk_validation_request'),
    ('ai_config_validations','endpoint_policy_revision_id','ai_endpoint_policies','fk_validation_endpoint'),
)


def _definitions():
    source = (Path(__file__).resolve().parents[1]/'sql/model_config_m1.sql').read_text(encoding='utf-8')
    source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
    result = [part.strip() for part in source.split(';') if part.strip()]
    if len(result) != 18 or any(not part.startswith('CREATE TABLE ') for part in result):
        raise RuntimeError('Invalid packaged M1 migration resource')
    return result


def trace_columns():
    return [sa.Column(name, kind, nullable=True) for name,kind in [
        ('release_id',sa.String(36)),('profile_revision_id',sa.String(36)),
        ('prompt_revision_id',sa.String(36)),('credential_revision_id',sa.String(36)),
        ('credential_version',sa.BigInteger()),('price_version_id',sa.String(36)),
        ('adapter_type',sa.String(32)),('provider_origin',sa.String(320)),
        ('workflow_id',sa.String(36)),('step_id',sa.String(36)),('usage_status',sa.String(24)),
        ('cost_amount',sa.Numeric(20,10)),('cost_currency',sa.String(3)),('pricing_status',sa.String(32)),
    ]] + [sa.Column('is_test',sa.Boolean(),nullable=False,server_default=sa.text('0'))]


def _verify_existing(bind, name):
    from app.model_config.tables import metadata
    expected = metadata.tables[name]
    inspector = sa.inspect(bind)
    actual = {row['name']:row for row in inspector.get_columns(name)}
    # Later, explicitly known extensions are not part of this revision's DDL.
    # Resume the original revision against either its own or the extended shape.
    later={'storage_binding'} if name=='rag_index_versions' else set()
    if set(actual)-later != set(expected.columns.keys())-later:
        raise RuntimeError('Existing M1 table shape differs: '+name)
    def type_sql(kind):
        result = str(kind.compile(dialect=bind.dialect)).upper()
        result = result.replace('BOOL','TINYINT(1)').replace('INTEGER','INT').replace('NUMERIC','DECIMAL')
        result = re.sub(r' (?:COLLATE|CHARACTER SET|CHARSET)\s+\w+','',result)
        return re.sub(r'\s+','',result)
    for column in expected.columns:
        if column.name in later and column.name not in actual:continue
        row = actual[column.name]
        if type_sql(row['type']) != type_sql(column.type) or row['nullable'] != column.nullable:
            raise RuntimeError('Existing M1 column type differs: '+name+'.'+column.name)
        expected_collation = getattr(column.type.dialect_impl(bind.dialect),'collation',None)
        if expected_collation and getattr(row['type'],'collation',None) != expected_collation:
            raise RuntimeError('Existing M1 column collation differs: '+name+'.'+column.name)
    expected_unique = {tuple(item.columns.keys()) for item in expected.constraints
                       if isinstance(item,sa.UniqueConstraint)}
    actual_unique = {tuple(item['column_names']) for item in inspector.get_unique_constraints(name)}
    if expected_unique != actual_unique:
        raise RuntimeError('Existing M1 unique constraints differ: '+name)
    if tuple(inspector.get_pk_constraint(name)['constrained_columns']) != tuple(expected.primary_key.columns.keys()):
        raise RuntimeError('Existing M1 primary key differs: '+name)
    if len(inspector.get_check_constraints(name)) != sum(isinstance(item,sa.CheckConstraint) for item in expected.constraints):
        raise RuntimeError('Existing M1 CHECK constraints differ: '+name)
    actual_fks = {item['name']:item for item in inspector.get_foreign_keys(name)}
    for constraint in expected.foreign_key_constraints:
        if constraint.use_alter:
            continue  # Applied after every table exists.
        actual_fk = actual_fks.get(constraint.name)
        elements = list(constraint.elements)
        if (not actual_fk or actual_fk['constrained_columns'] != list(constraint.column_keys)
                or actual_fk['referred_table'] != elements[0].column.table.name
                or actual_fk['referred_columns'] != [element.column.name for element in elements]):
            raise RuntimeError('Existing M1 foreign key differs: '+name+'.'+constraint.name)


def upgrade():
    offline = context.is_offline_mode()
    inspector = None if offline else sa.inspect(op.get_bind())
    collation = 'utf8mb4_unicode_ci'
    if not offline:
        bind = op.get_bind()
        if bind.dialect.name != 'mysql':
            raise RuntimeError('M1 migrations require MySQL 8; SQLite is only used for logic tests')
        version = bind.exec_driver_sql('SELECT VERSION()').scalar_one()
        numbers = tuple(int(part) for part in re.match(r'^(\d+)\.(\d+)\.(\d+)',version).groups())
        if numbers < (8,0,16) or 'MariaDB' in version:
            raise RuntimeError('M1 requires enforced CHECK constraints in MySQL 8.0.16+')
        legacy = bind.execute(sa.text("SELECT CHARACTER_SET_NAME,COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='users' AND COLUMN_NAME='uuid'")).one()
        if legacy[0] != 'utf8mb4' or not re.fullmatch(r'utf8mb4_[a-z0-9_]+',legacy[1]):
            raise RuntimeError('users.uuid must use a verified utf8mb4 collation before M1 migration')
        collation = legacy[1]  # Match existing identifiers without rewriting legacy data.
    for statement in _definitions():
        name = re.match(r'CREATE TABLE (\w+)',statement).group(1)
        if offline or not inspector.has_table(name):
            op.execute(statement.replace('COLLATE=utf8mb4_unicode_ci','COLLATE='+collation))
        else:
            # A partially applied MySQL DDL migration is resumable, an unrelated
            # same-name table is not. Refuse schema drift instead of stamping it.
            _verify_existing(op.get_bind(),name)
    for table,column,target,name in LATE_FOREIGN_KEYS:
        existing_fks = set() if offline else {
            item['name'] for item in sa.inspect(op.get_bind()).get_foreign_keys(table)}
        if name not in existing_fks:
            op.create_foreign_key(name,table,target,
                ['scope_type','scope_id',column],['scope_type','scope_id','id'])
    existing = set() if offline else {column['name'] for column in inspector.get_columns('model_traces')}
    for column in trace_columns():
        if column.name not in existing:
            op.add_column('model_traces',column)
    if not offline:
        # Existing traces have no defensible historical price revision.
        op.execute(sa.text("UPDATE model_traces SET pricing_status='legacy_estimate' WHERE pricing_status IS NULL"))


def downgrade():
    definitions = _definitions()
    names = [re.match(r'CREATE TABLE (\w+)',statement).group(1) for statement in definitions]
    if not context.is_offline_mode():
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        # Never silently discard configured credentials/releases during code rollback.
        for name in names:
            if inspector.has_table(name) and bind.exec_driver_sql(f'SELECT 1 FROM {name} LIMIT 1').first():
                raise RuntimeError('M1 configuration data exists; export and retire it before schema downgrade')
    for table,column,target,name in reversed(LATE_FOREIGN_KEYS):
        op.drop_constraint(name,table,type_='foreignkey')
    for column in reversed(trace_columns()):
        op.drop_column('model_traces',column.name)
    for name in reversed(names):
        op.drop_table(name)
