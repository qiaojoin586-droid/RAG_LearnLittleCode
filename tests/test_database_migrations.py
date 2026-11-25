"""无外部数据库的遗留库迁移回归：DDL 去重与存量分类保留。"""
import asyncio
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db import database


@pytest.mark.parametrize('binding,trace,refused',[
    ({'adapter':'chroma_local'},None,'Pinned index storage'),
    (None,{'index_version_id':'index','purpose':'query'},'Index trace provenance'),
    (None,None,None),
])
def test_index_storage_migration_preserves_bound_storage_and_trace_provenance(binding,trace,refused):
    migration=load_migration('c346e7f8a9b0_index_storage_binding.py')
    engine=sa.create_engine('sqlite://');metadata=sa.MetaData()
    indexes=sa.Table('rag_index_versions',metadata,sa.Column('id',sa.Integer,primary_key=True))
    traces=sa.Table('model_traces',metadata,sa.Column('id',sa.Integer,primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        migration.context=SimpleNamespace(is_offline_mode=lambda:False)
        migration.op=Operations(MigrationContext.configure(connection))
        migration.upgrade();migration.upgrade()
        extended=sa.MetaData();extended.reflect(connection)
        connection.execute(extended.tables['rag_index_versions'].insert().values(id=1,storage_binding=binding))
        connection.execute(extended.tables['model_traces'].insert().values(id=1,index_context=trace))
        if refused:
            with pytest.raises(RuntimeError,match=refused):migration.downgrade()
            assert 'storage_binding' in {c['name'] for c in sa.inspect(connection).get_columns(indexes.name)}
            assert 'index_context' in {c['name'] for c in sa.inspect(connection).get_columns(traces.name)}
        else:
            migration.downgrade();migration.upgrade()
            assert connection.exec_driver_sql('SELECT id FROM model_traces').scalar_one()==1
    engine.dispose()


def load_migration(filename):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tables,expected_stamp", [
    ([], None),
    (["users", "notes"], "06f20a30109b"),
    (["users", "notes", "alembic_version"], None),
])
def test_legacy_database_runs_incremental_migrations(monkeypatch, tables, expected_stamp):
    monkeypatch.setattr(database, "inspect", lambda conn: SimpleNamespace(get_table_names=lambda: tables))

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run_sync(self, callback):
            return callback(None)

    monkeypatch.setattr(database, "engine", SimpleNamespace(connect=Connection))
    stamp, upgrade = MagicMock(), MagicMock()
    monkeypatch.setattr(command, "stamp", stamp)
    monkeypatch.setattr(command, "upgrade", upgrade)
    asyncio.run(database.init_db())
    if expected_stamp:
        assert stamp.call_args.args[1] == expected_stamp
    else:
        stamp.assert_not_called()
    assert upgrade.call_args.args[1] == "head"


@pytest.mark.parametrize("existing", [False, True])
def test_category_migration_adds_only_missing_schema(monkeypatch, existing):
    migration = load_migration("a3f8d2c1b9e4_add_note_categories.py")
    inspector = SimpleNamespace(
        has_table=lambda table: existing,
        get_columns=lambda table: [{"name": "category_id"}] if existing else [],
        get_indexes=lambda table: [{"name": name} for name in (
            ["ix_notes_category_id"] if table == "notes"
            else ["ix_note_categories_user_id", "ix_note_categories_parent_id"]
        )] if existing else [],
        get_foreign_keys=lambda table: [{
            "constrained_columns": ["category_id"], "referred_table": "note_categories",
        }] if existing else [],
    )
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.sa, "inspect", lambda bind: inspector)
    operations = MagicMock()
    migrate_data = MagicMock()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "migrate_category_data", migrate_data)
    migration.upgrade()
    assert operations.create_table.call_count == int(not existing)
    assert operations.add_column.call_count == int(not existing)
    assert operations.create_index.call_count == (0 if existing else 3)
    assert operations.create_foreign_key.call_count == int(not existing)
    assert migrate_data.call_count == int(not existing)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("filename,field,ddl", [
    ("b7e2c9d4a5f6_add_note_fulltext_index.py", "ft_notes_title_content", "execute"),
    ("e8a1b2c3d4f5_add_note_format.py", "format", "add_column"),
])
def test_note_migrations_preserve_existing_index_and_format(monkeypatch, existing, filename, field, ddl):
    migration = load_migration(filename)
    entries = [{"name": field}] if existing else []
    inspector = SimpleNamespace(get_indexes=lambda table: entries, get_columns=lambda table: entries)
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.sa, "inspect", lambda bind: inspector)
    operations = MagicMock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert getattr(operations, ddl).call_count == int(not existing)


def test_incremental_migrations_still_generate_complete_mysql_sql(monkeypatch):
    output = StringIO()
    context = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})
    for filename in (
        "a3f8d2c1b9e4_add_note_categories.py",
        "b7e2c9d4a5f6_add_note_fulltext_index.py",
        "e8a1b2c3d4f5_add_note_format.py",
    ):
        migration = load_migration(filename)
        monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE note_categories" in sql
    assert "ADD COLUMN category_id" in sql
    assert "FULLTEXT INDEX ft_notes_title_content" in sql
    assert "ADD COLUMN format" in sql


def test_category_backfill_reuses_existing_tree_and_preserves_assignments():
    migration = load_migration("a3f8d2c1b9e4_add_note_categories.py")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("uuid", sa.String, primary_key=True))
    notes = sa.Table("notes", metadata,
                     sa.Column("id", sa.String, primary_key=True),
                     sa.Column("user_id", sa.String), sa.Column("category", sa.String),
                     sa.Column("category_id", sa.String))
    categories = sa.Table("note_categories", metadata,
                          sa.Column("id", sa.String, primary_key=True),
                          sa.Column("user_id", sa.String), sa.Column("parent_id", sa.String),
                          sa.Column("name", sa.String), sa.Column("sort_order", sa.Integer),
                          sa.Column("deleted_at", sa.DateTime))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(uuid="user"))
        conn.execute(categories.insert().values(id="work", user_id="user", name="工作", sort_order=0))
        conn.execute(notes.insert(), [
            {"id": "unmapped", "user_id": "user", "category": "工作", "category_id": None},
            {"id": "assigned", "user_id": "user", "category": "学习", "category_id": "work"},
        ])
        migration.migrate_category_data(conn)
        first_tree = conn.execute(sa.select(categories)).all()
        migration.migrate_category_data(conn)
        assert conn.execute(sa.select(categories)).all() == first_tree
        assert dict(conn.execute(sa.select(notes.c.id, notes.c.category_id)).all()) == {
            "unmapped": "work", "assigned": "work",
        }
    engine.dispose()
