"""Execute the design DDL in a disposable MySQL 8 instance, never the application DB.

Requires an installed mysqld binary and PyMySQL. No application imports, .env reads,
service control, or production credentials. The only server started uses a new data
directory under the workspace, a temporary loopback port, and disabled MySQL X.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymysql


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--mysqld', required=True)
    parser.add_argument('--with-memory', action='store_true', help='Also execute v1.3 memory extension and checks')
    parser.add_argument('--with-audit-migration', action='store_true', help='Exercise the implemented audit-only Alembic migration')
    parser.add_argument('--with-config-migration', action='store_true', help='Exercise M1 migration and control-plane transactions in a second isolated fixture')
    args = parser.parse_args()
    executable = Path(args.mysqld).resolve(strict=True)
    workspace = Path(__file__).resolve().parents[3]
    ddl_path = Path(__file__).resolve().parents[1] / '02_数据模型DDL.sql'
    gate_root_dir = workspace / '.cache' / ('upgrade-m1-mysql' if args.with_config_migration else 'upgrade-m0-mysql' if args.with_audit_migration else
                                          'plan-ddl-v13' if args.with_memory else 'plan-ddl-v12')
    run_dir = gate_root_dir / uuid.uuid4().hex
    run_dir.mkdir(parents=True)
    data_dir, temp_dir = run_dir / 'data', run_dir / 'tmp'
    temp_dir.mkdir()
    log_path = run_dir / 'server.log'
    report = {'ddl_sha256': hashlib.sha256(ddl_path.read_bytes()).hexdigest(),
              'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'checks': {},
              'fixture_scope': 'minimal legacy tables; not production migration or application tests'}
    extension_path = ddl_path.parent / '08_个人记忆文件DDL.sql'
    if args.with_memory:
        report['memory_extension_sha256'] = hashlib.sha256(extension_path.read_bytes()).hexdigest()
    if args.with_audit_migration:
        report['fixture_scope'] += '; audit-only Alembic migration, not the full application migration chain'
    server = None
    conn = None
    init_user_path = run_dir / 'init-user.sql'
    gate_password = uuid.uuid4().hex
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        with log_path.open('wb') as output:
            subprocess.run([str(executable), '--no-defaults', '--initialize-insecure', '--console',
                            f'--basedir={executable.parent.parent}', f'--datadir={data_dir}',
                            f'--tmpdir={temp_dir}'],
                           stdout=output, stderr=subprocess.STDOUT, check=True,
                           timeout=90, creationflags=flags)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            init_user_path.write_text("CREATE USER 'gate_root'@'127.0.0.1' IDENTIFIED BY '" + gate_password + "';\nGRANT ALL PRIVILEGES ON *.* TO 'gate_root'@'127.0.0.1' WITH GRANT OPTION;\n", encoding='utf-8')
            server = subprocess.Popen([str(executable), '--no-defaults',
                f'--basedir={executable.parent.parent}', f'--datadir={data_dir}',
                f'--tmpdir={temp_dir}', f'--log-error={run_dir / "mysql-error.log"}',
                f'--pid-file={run_dir / "mysql.pid"}', '--bind-address=127.0.0.1',
                f'--init-file={init_user_path}',
                f'--port={port}', '--mysqlx=OFF', '--skip-name-resolve',
                '--innodb-buffer-pool-size=32M', '--max-connections=10',
                '--performance-schema=OFF'], stdout=output, stderr=subprocess.STDOUT,
                creationflags=flags)
            deadline = time.monotonic() + 40
            connection_error = None
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError(f'Isolated mysqld exited; see {run_dir}')
                try:
                    conn = pymysql.connect(host='127.0.0.1', port=port, user='gate_root',
                                           password=gate_password, connect_timeout=1,
                                           charset='utf8mb4', autocommit=True)
                    break
                except pymysql.MySQLError as exc:
                    connection_error = exc
                    time.sleep(0.25)
            if conn is None:
                raise RuntimeError(f'Isolated MySQL connection unavailable: {connection_error}')
            cur = conn.cursor()
            cur.execute('SELECT VERSION()')
            report['mysql_version'] = cur.fetchone()[0]
            if not report['mysql_version'].startswith('8.0.'):
                raise RuntimeError('This gate targets MySQL 8.0')
            cur.execute('CREATE DATABASE plan_fixture CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
            cur.execute('USE plan_fixture')
            cur.execute("CREATE TABLE users (uuid VARCHAR(36) PRIMARY KEY) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
            cur.execute("CREATE TABLE model_traces (id BIGINT PRIMARY KEY, request_id VARCHAR(32)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
            cur.execute("CREATE TABLE tool_call_audit (id BIGINT PRIMARY KEY) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
            # This DDL contains no stored routines or semicolons inside SQL literals.
            sql_paths = [ddl_path] + ([extension_path] if args.with_memory else [])
            text = '\n'.join(line for sql_path in sql_paths for line in sql_path.read_text(encoding='utf-8').splitlines()
                             if not line.lstrip().startswith('--'))
            statements = [part.strip() for part in text.split(';') if part.strip()]
            for ordinal, statement in enumerate(statements, 1):
                try:
                    cur.execute(statement)
                except pymysql.MySQLError as exc:
                    raise RuntimeError(f'DDL statement {ordinal} failed: {exc.args}') from exc
            report['ddl_statements'] = len(statements)
            report['checks']['full_ddl_executes'] = True
            cur.execute("SELECT TABLE_NAME,TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA='plan_fixture'")
            collations = dict(cur.fetchall())
            assert set(collations.values()) == {'utf8mb4_unicode_ci'}, collations
            report['new_table_count'] = len(collations) - 3
            report['checks']['table_collations_match_legacy_fixture'] = True

            def expect_error(sql, values, expected):
                try:
                    cur.execute(sql, values)
                except pymysql.MySQLError as exc:
                    assert exc.args[0] == expected, exc.args
                    return
                raise AssertionError(f'Expected MySQL error {expected}')

            expect_error('CREATE TABLE reserved_word_probe (last_value BIGINT)', None, 1064)
            report['checks']['original_reserved_word_error_reproduced'] = True
            cur.execute("INSERT INTO users VALUES ('actor')")
            cur.execute("INSERT INTO ai_config_admin_grants VALUES ('system','system','actor','config_admin','active','actor',UTC_TIMESTAMP(6),NULL)")
            cur.execute('SELECT COUNT(*) FROM users u JOIN ai_config_admin_grants g ON u.uuid=g.subject_user_id')
            assert cur.fetchone()[0] == 1
            report['checks']['legacy_new_identifier_join'] = True
            expect_error("INSERT INTO ai_config_admin_grants VALUES ('system','system','bad','planner','active','actor',UTC_TIMESTAMP(6),NULL)", None, 3819)
            report['checks']['closed_enum_check_rejects_business_role_as_permission_role'] = True
            cur.execute("INSERT INTO ai_version_counters VALUES ('system','system','profile','fixture',0,UTC_TIMESTAMP(6))")
            cur.execute("UPDATE ai_version_counters SET last_num=last_num+1,updated_at=UTC_TIMESTAMP(6) WHERE scope_type='system' AND scope_id='system' AND entity_kind='profile' AND entity_key='fixture'")
            cur.execute("SELECT last_num FROM ai_version_counters WHERE scope_type='system' AND scope_id='system' AND entity_kind='profile' AND entity_key='fixture' FOR UPDATE")
            assert cur.fetchone()[0] == 1
            expect_error("INSERT INTO ai_version_counters VALUES ('system','system','release','forbidden',0,UTC_TIMESTAMP(6))", None, 3819)
            report['checks']['counter_allocates_and_rejects_release_kind'] = True

            # These rows exercise relational/CAS mechanics, not full business schemas.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            def insert(table, values):
                columns = ','.join(values)
                cur.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join(["%s"] * len(values))})', list(values.values()))
            def draft(identifier):
                insert('ai_config_drafts', dict(id=identifier, scope_type='system', scope_id='system',
                    base_head_version=0, draft_version=1, payload='{}', payload_hash='a'*64,
                    status='ready', expires_at=now+timedelta(days=7), created_by='actor',
                    created_at=now, updated_at=now))
            def request(identifier, scope_type='system', scope_id='system'):
                insert('ai_config_requests', dict(id=identifier, scope_type=scope_type, scope_id=scope_id,
                    actor_id='actor', action='bootstrap.import', request_key=identifier,
                    action_payload='{}', execution_state='{}', payload_hash='a'*64,
                    payload_hash_key_version='fixture-hmac-v1', status='running',
                    deadline_at=now+timedelta(minutes=10), created_at=now,
                    replay_until=now+timedelta(days=7)))
            def release(identifier, draft_id, request_id, number):
                return dict(id=identifier, scope_type='system', scope_id='system', release_number=number,
                    draft_id=draft_id, draft_version=1, request_record_id=request_id, schema_version=1,
                    role_bindings_raw='{}', resolved_static_bindings='{}', follow_execution_roles='[]',
                    index_bindings='{}', validation_refs='[]', content_hash='a'*64,
                    created_by='actor', created_at=now)
            draft('draft-a'); request('request-a')
            cur.execute("INSERT INTO ai_config_heads(scope_type,scope_id,active_release_id,head_version,last_release_number,updated_at) VALUES ('system','system',NULL,0,0,UTC_TIMESTAMP(6))")
            conn.begin()
            cur.execute("SELECT last_release_number FROM ai_config_heads WHERE scope_type='system' AND scope_id='system' FOR UPDATE")
            next_number = cur.fetchone()[0] + 1
            insert('ai_config_releases', release('release-a', 'draft-a', 'request-a', next_number))
            cur.execute("UPDATE ai_config_heads SET active_release_id='release-a',head_version=head_version+1,last_release_number=%s WHERE scope_type='system' AND scope_id='system' AND head_version=0", (next_number,))
            assert cur.rowcount == 1
            conn.commit()
            cur.execute("UPDATE ai_config_heads SET head_version=head_version+1 WHERE scope_type='system' AND scope_id='system' AND head_version=0")
            assert cur.rowcount == 0
            report['checks']['initial_head_and_stale_cas'] = True
            draft('draft-cross'); request('request-foreign', 'user', 'foreign')
            cross = release('release-cross', 'draft-cross', 'request-foreign', 2)
            try:
                insert('ai_config_releases', cross)
                raise AssertionError('Cross-scope request reference accepted')
            except pymysql.MySQLError as exc:
                assert exc.args[0] == 1452, exc.args
            report['checks']['cross_scope_release_request_fk_rejects'] = True

            password = uuid.uuid4().hex
            cur.execute("CREATE USER 'gate_actor'@'127.0.0.1' IDENTIFIED BY %s", (password,))
            cur.execute("GRANT SELECT,INSERT ON plan_fixture.ai_secrets TO 'gate_actor'@'127.0.0.1'")
            cur.execute("GRANT UPDATE(status,revocation_epoch,revoked_at,retired_at,candidate_expires_at) ON plan_fixture.ai_secrets TO 'gate_actor'@'127.0.0.1'")
            with pymysql.connect(host='127.0.0.1', port=port, user='gate_actor', password=password,
                                  database='plan_fixture', autocommit=True) as restricted:
                with restricted.cursor() as check:
                    check.execute("UPDATE ai_secrets SET status='retired' WHERE id='none'")
                    try:
                        check.execute("UPDATE ai_secrets SET ciphertext='forbidden' WHERE id='none'")
                        raise AssertionError('Ciphertext UPDATE unexpectedly allowed')
                    except pymysql.MySQLError as exc:
                        assert exc.args[0] in (1142,1143), exc.args
            report['checks']['column_grant_blocks_ciphertext_update'] = True
            if args.with_memory:
                cur.execute("INSERT INTO users VALUES ('other')")
                for owner in ('actor','other'):
                    insert('user_memory_heads', dict(user_id=owner,created_at=now,updated_at=now))
                def memory_revision(identifier,owner,number,content):
                    return dict(id=identifier,user_id=owner,revision_number=number,content_md=content,
                                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                                source_kind='user_edit',created_at=now)
                insert('user_memory_revisions',memory_revision('memory-a','actor',1,'# 回答偏好\n默认中文'))
                cur.execute("UPDATE user_memory_heads SET active_revision_id='memory-a',head_version=1,last_revision_number=1 WHERE user_id='actor'")
                expect_error("UPDATE user_memory_heads SET active_revision_id='memory-a' WHERE user_id='other'",None,1452)
                report['checks']['memory_head_cross_user_revision_rejected'] = True
                cur.execute("UPDATE user_memory_heads SET head_version=head_version+1 WHERE user_id='actor' AND head_version=0")
                assert cur.rowcount == 0
                report['checks']['memory_stale_cas_no_update'] = True
                try:
                    insert('user_memory_revisions',memory_revision('too-large','actor',2,'😀'*9000))
                    raise AssertionError('Memory UTF-8 byte limit accepted')
                except pymysql.MySQLError as exc:
                    assert exc.args[0] == 3819,exc.args
                report['checks']['memory_utf8_byte_limit_rejected'] = True
                job=dict(id='job-a',user_id='actor',source_request_id='a'*32,base_revision_id='memory-a',
                    expected_head_version=1,memory_epoch=0,scope_type='system',scope_id='system',
                    config_release_id='release-a',status='queued',deadline_at=now+timedelta(minutes=3),
                    created_at=now,updated_at=now)
                insert('user_memory_jobs',job)
                duplicate=dict(job,id='job-duplicate')
                try:
                    insert('user_memory_jobs',duplicate)
                    raise AssertionError('Duplicate memory source accepted')
                except pymysql.MySQLError as exc:
                    assert exc.args[0] == 1062,exc.args
                report['checks']['memory_source_request_unique'] = True
                expect_error("UPDATE user_memory_jobs SET status='unchecked' WHERE id='job-a'",None,3819)
                expect_error("UPDATE user_memory_jobs SET scope_id='other' WHERE id='job-a'",None,1452)
                report['checks']['memory_job_closed_state_and_release_scope'] = True
                cur.execute("UPDATE user_memory_heads SET memory_epoch=memory_epoch+1,head_version=head_version+1,enabled=0 WHERE user_id='actor'")
                cur.execute("UPDATE user_memory_heads SET head_version=head_version+1 WHERE user_id='actor' AND memory_epoch=0 AND head_version=1")
                assert cur.rowcount == 0
                report['checks']['memory_epoch_fences_old_job_commit'] = True
            if args.with_audit_migration:
                import importlib.util
                from types import SimpleNamespace
                import sqlalchemy as sa
                from sqlalchemy.pool import NullPool
                from alembic.migration import MigrationContext
                from alembic.operations import Operations
                migration_path = workspace / 'alembic/versions/f1c2d3e4a5b6_tool_audit_correlation.py'
                report['audit_migration_sha256'] = hashlib.sha256(migration_path.read_bytes()).hexdigest()
                spec = importlib.util.spec_from_file_location('audit_migration_gate', migration_path)
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                # Only this freshly created fixture table is altered. Existing design
                # DDL has the new columns; first restore its legacy shape for the gate.
                for column in migration.FIELDS:
                    cur.execute(f'ALTER TABLE tool_call_audit DROP COLUMN {column}')
                cur.execute('INSERT INTO tool_call_audit(id) VALUES (1)')
                def migration_connection():
                    return pymysql.connect(host='127.0.0.1', port=port, user='gate_root',
                        password=gate_password, database='plan_fixture', charset='utf8mb4')
                migration_engine = sa.create_engine('mysql+pymysql://', creator=migration_connection,
                                                   poolclass=NullPool)
                try:
                    with migration_engine.begin() as connection:
                        migration.context = SimpleNamespace(is_offline_mode=lambda: False)
                        migration.op = Operations(MigrationContext.configure(connection))
                        migration.upgrade()
                        migration.upgrade()
                        assert set(migration.FIELDS).issubset({column['name'] for column in
                            sa.inspect(connection).get_columns('tool_call_audit')})
                        migration.downgrade()
                        assert connection.exec_driver_sql('SELECT id FROM tool_call_audit').all() == [(1,)]
                        migration.upgrade()
                    report['checks']['audit_alembic_up_down_up_preserves_legacy_row'] = True
                finally:
                    migration_engine.dispose()
            if args.with_config_migration:
                from model_config_mysql_gate import run_gate
                run_gate(workspace,port,gate_password,report)
            report['passed'] = True
    except Exception as exc:
        report['passed'] = False
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['log_tail'] = log_path.read_text(encoding='utf-8', errors='replace')[-3000:] if log_path.exists() else ''
        raise
    finally:
        if conn:
            try:
                with conn.cursor() as shutdown:
                    shutdown.execute('SHUTDOWN')
            except pymysql.MySQLError:
                pass
            conn.close()
        if server and server.poll() is None:
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.wait(timeout=10)
        report['owned_server_stopped'] = server is None or server.poll() is not None
        if init_user_path.exists():
            init_user_path.unlink()
        (run_dir/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        # Only a task-created, verified child directory can be removed.
        if (report['owned_server_stopped'] and data_dir.exists() and not data_dir.is_symlink()
                and data_dir.resolve().is_relative_to(run_dir.resolve())
                and run_dir.resolve().is_relative_to(gate_root_dir.resolve())):
            try:
                shutil.rmtree(data_dir)
            except OSError as exc:
                report['cleanup_pending'] = type(exc).__name__
                (run_dir/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'report':str(run_dir/'result.json'), **report}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
