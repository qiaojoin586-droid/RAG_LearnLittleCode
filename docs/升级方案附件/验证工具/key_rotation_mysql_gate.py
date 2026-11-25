"""Real MySQL material-version accounting and publication failure atomicity."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile

import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_key_rotation_gate(workspace,engine,Database,actor,control,report):
    from app.core.failed_response import BusinessError
    from app.model_config import tables as t
    from app.model_config.contracts import SCHEMA
    from app.model_config.crypto import Keyring
    from app.model_config.key_rotation import prepare_rotation,inspect_keyring
    from app.model_config.publishing import PublishingService
    from app.model_config.security import issue_proof
    @asynccontextmanager
    async def database():
        with Session(engine) as session:yield Database(session)
    with tempfile.TemporaryDirectory(prefix='key-rotation-',dir=workspace/'.cache/upgrade-m1-mysql') as directory:
        original=control.keyring;source=Path(directory)/'original.json';output=Path(directory)/'next.json'
        source.write_text(json.dumps({'active_version':original.active_version,'keys':{
            key:value.decode() for key,value in original.keys.items()}}),encoding='utf-8')
        before=source.read_bytes();prepare_rotation(source,output,'mysql-master-v2');rotated=Keyring.load(output)
        assert source.read_bytes()==before
        with Session(engine) as session:
            inventory=asyncio.run(inspect_keyring(Database(session),actor,rotated,check_decryption=True))
        assert inventory['references_available'] and inventory['credential_decryption_check_passed']
        assert inventory['memory_reference_table_present'] and any(item['memory_replay_requests'] for item in inventory['versions'])
        report['checks']['key_rotation_mysql_inventory_checks_config_memory_hmac_and_credential_versions']=True
        with engine.connect() as connection:
            head=dict(connection.execute(sa.select(t.ai_config_heads)).mappings().one())
            release=connection.execute(sa.select(t.ai_config_releases).where(t.ai_config_releases.c.id==head['active_release_id'])).mappings().one()
            binding=release['resolved_static_bindings']['chat']
            profile=dict(connection.execute(sa.select(t.ai_profile_revisions).where(t.ai_profile_revisions.c.id==binding['profile_revision_id'])).mappings().one())
            old_secret=dict(connection.execute(sa.select(t.ai_secrets).where(t.ai_secrets.c.id==binding['credential_revision_id'])).mappings().one())
        control._keyring=rotated;proof=issue_proof(rotated,actor,1)['proof']
        with Session(engine) as session:
            draft,_=asyncio.run(control.create_draft(Database(session),actor,{'base_release_id':head['active_release_id'],
                'expected_head_version':head['head_version']},'key-rotation-draft'))
        profile={key:profile[key] for key in SCHEMA['$defs']['ProfileInput']['properties']}
        # Previous gate keeps this role disabled; this is a metadata publication.
        # Actual old/new HTTP requests are tested with SQLite and mock transport.
        with Session(engine) as session:
            draft,_=asyncio.run(control.patch_draft(Database(session),actor,draft['id'],{'operations':[{
                'op':'replace_profile','role_key':'chat','profile':profile,
                'credential':{'action':'replace','api_key':'fictional-new-provider-key'}}]},'key-rotation-profile',draft['etag'],proof))
        new_secret=draft['payload']['role_bindings_raw']['chat']['credential_revision_id']
        with Session(engine) as session:
            published,_=asyncio.run(PublishingService(control,database).publish(Database(session),actor,draft['id'],{
                'expected_draft_version':draft['draft_version'],'expected_head_version':head['head_version']},'key-rotation-publish',proof,draft['etag']))
        with engine.connect() as connection:
            old=dict(connection.execute(sa.select(t.ai_secrets).where(t.ai_secrets.c.id==old_secret['id'])).mappings().one())
            new=connection.execute(sa.select(t.ai_secrets).where(t.ai_secrets.c.id==new_secret)).mappings().one()
            assert old['ciphertext']==old_secret['ciphertext'] and old['key_version']==old_secret['key_version']
            assert new['key_version']=='mysql-master-v2' and new['credential_version']>old['credential_version']
            releases=connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_releases)).scalar_one()
        report['checks']['key_rotation_mysql_new_ciphertext_and_release_keep_original_row_immutable']=True
        with Session(engine) as session:
            try:asyncio.run(PublishingService(control,database).restore(Database(session),actor,head['active_release_id'],
                {'expected_head_version':published['head_version']},'key-rotation-revoked-restore',proof))
            except BusinessError as error:assert error.code==40926
            else:raise AssertionError('Retained master keys cannot revive a revoked credential')
        report['checks']['key_rotation_mysql_retained_keys_do_not_restore_revoked_credential']=True
        control._keyring=original;old_proof=issue_proof(original,actor,1)['proof']
        try:
            with Session(engine) as session:
                try:asyncio.run(PublishingService(control,database).restore(Database(session),actor,published['release_id'],
                    {'expected_head_version':published['head_version']},'key-rotation-missing-master',old_proof))
                except BusinessError as error:assert error.code==50303
                else:raise AssertionError('Missing master version must fail before publishing')
            with engine.connect() as connection:
                assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==published['head_version']
                assert connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_releases)).scalar_one()==releases
        finally:control._keyring=rotated
        report['checks']['key_rotation_mysql_missing_master_rolls_back_restore_without_head_move']=True
