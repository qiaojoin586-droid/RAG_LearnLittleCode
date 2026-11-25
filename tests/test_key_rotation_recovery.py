"""Staged file rotation, immutable credentials, old requests and isolated recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.crypto import Keyring
from app.model_config.key_rotation import prepare_rotation,inspect_keyring
from app.model_config.publishing import PublishingService
from app.model_config.security import verify_proof
from app.model_config.service import ConfigService,utcnow,uid
from app.model_config.snapshots import load_snapshot
from test_model_config_control_plane import store,admin,AsyncFacade,profile_op  # noqa: F401
from test_model_config_publication import ready,publish,snapshot,selected,mock_runtime,database_factory
from test_model_config_validation import validation_service,execute
from test_credential_revocation import revoke
from test_user_memory import memory,save  # noqa: F401


def files(tmp_path):
    source=tmp_path/'old.json';old=Keyring.create(source);target=tmp_path/'next.json'
    return source,old,target


def staged(admin,tmp_path):
    source=tmp_path/'source.json'
    source.write_text(json.dumps({'active_version':admin.keyring.active_version,
        'keys':{key:value.decode() for key,value in admin.keyring.keys.items()}}),encoding='utf-8')
    output=tmp_path/'rotated.json';prepare_rotation(source,output,'master-v2')
    return Keyring.load(output)


async def inspect(admin,keyring,check=True):
    with Session(admin.engine) as session:
        return await inspect_keyring(AsyncFacade(session),admin.actor,keyring,check_decryption=check)


def test_staged_file_retains_old_material_encrypts_new_version_and_never_modifies_source(tmp_path):
    source,old,target=files(tmp_path);original=source.read_bytes()
    identity={'id':'fixture','origin':'https://example.com'}
    encrypted,version=old.encrypt('private-fixture-value',identity)
    result=prepare_rotation(source,target,'master-v2');new=Keyring.load(target)
    assert source.read_bytes()==original and new.keys['local-v1']==old.keys['local-v1']
    assert new.active_version=='master-v2' and new.decrypt(encrypted,version,identity)=='private-fixture-value'
    fresh,fresh_version=new.encrypt('another-value',identity)
    assert fresh_version=='master-v2' and new.decrypt(fresh,fresh_version,identity)=='another-value'
    assert result['activation_required'] and not result['existing_credentials_rewritten']
    assert all(value.decode() not in json.dumps(result) for value in new.keys.values())
    assert not list(tmp_path.glob('.keyring-rotation-*'))


def test_existing_target_source_same_path_and_duplicate_version_are_rejected(tmp_path):
    source,old,target=files(tmp_path);target.write_text('preserve-existing-file',encoding='utf-8')
    for output,version in [(target,'master-v2'),(source,'master-v2'),(tmp_path/'unused.json','local-v1')]:
        with pytest.raises(BusinessError):prepare_rotation(source,output,version)
    assert target.read_text(encoding='utf-8')=='preserve-existing-file' and Keyring.load(source).keys==old.keys


def test_concurrent_preparation_has_one_complete_winner_and_no_temp_key_files(tmp_path):
    source,_,target=files(tmp_path)
    def run(version):
        try:return prepare_rotation(source,target,version)
        except (BusinessError,FileExistsError):return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['one','two']))
    assert sum(result is not None for result in results)==1
    assert Keyring.load(target).active_version in ('one','two') and len(Keyring.load(target).keys)==2
    assert not list(tmp_path.glob('.keyring-rotation-*'))


def test_failed_flush_leaves_original_intact_and_no_output_or_temp_secrets(tmp_path,monkeypatch):
    source,_,target=files(tmp_path);original=source.read_bytes()
    def broken(_):raise OSError('simulated filesystem failure')
    monkeypatch.setattr(os,'fsync',broken)
    with pytest.raises(OSError):prepare_rotation(source,target,'next')
    assert source.read_bytes()==original and not target.exists() and not list(tmp_path.glob('.keyring-rotation-*'))


@pytest.mark.parametrize('kind',['duplicate_root','duplicate_version','extra_field','bad_shape','invalid_key','oversize'])
def test_keyring_load_rejects_ambiguous_or_invalid_files_without_leaking_exception_context(tmp_path,kind):
    path=tmp_path/'invalid.json';key=Fernet.generate_key().decode()
    values={'duplicate_root':'{"active_version":"v1","active_version":"v2","keys":{"v2":"'+key+'"}}',
        'duplicate_version':'{"active_version":"v1","keys":{"v1":"'+key+'","v1":"'+key+'"}}',
        'extra_field':json.dumps({'active_version':'v1','keys':{'v1':key},'secret_extra':'must-not-echo'}),
        'bad_shape':'[]','invalid_key':json.dumps({'active_version':'v1','keys':{'v1':'must-not-echo'}}),
        'oversize':' '*1048577}
    path.write_text(values[kind],encoding='utf-8')
    with pytest.raises(BusinessError) as error:Keyring.load(path)
    assert error.value.code==50303 and error.value.__context__ is None
    assert 'must-not-echo' not in str(error.value)


def test_cli_prepare_uses_no_database_and_emits_only_metadata(tmp_path):
    source,_,target=files(tmp_path)
    command=[sys.executable,'-m','app.model_config.cli','prepare-keyring-rotation','--source',str(source),
        '--output',str(target),'--new-version','master-v2']
    environment={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',env=environment,timeout=30)
    assert result.returncode==0 and json.loads(result.stdout)['status']=='prepared'
    assert all(value.decode() not in result.stdout+result.stderr for value in Keyring.load(target).keys.values())
    second=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',env=environment,timeout=30)
    assert second.returncode!=0 and 'Traceback' not in second.stderr


@pytest.mark.parametrize('available,failed,code',[(False,0,2),(True,1,2),(True,0,0)])
def test_inspect_cli_exit_status_exposes_failed_checks_to_automation(monkeypatch,capsys,available,failed,code):
    from app.model_config.cli import main
    monkeypatch.setattr('dotenv.load_dotenv',lambda *args,**kwargs:None)
    monkeypatch.setattr(sys,'argv',['model-config','inspect-keyring','--user-id','fixture-user'])
    def checked(coroutine):
        coroutine.close();return {'references_available':available,'failed_credentials':failed}
    monkeypatch.setattr(asyncio,'run',checked)
    if code:
        with pytest.raises(SystemExit) as error:main()
        assert error.value.code==code
    else:main()
    assert json.loads(capsys.readouterr().out)['failed_credentials']==failed


@pytest.mark.asyncio
async def test_inventory_checks_crypto_separately_from_version_availability_and_has_no_writes(admin,tmp_path):
    draft=await ready(admin,local=False);await publish(admin,draft)
    new=staged(admin,tmp_path);before=[dict(row) for row in admin.rows(t.ai_secrets)]
    report=await inspect(admin,new)
    assert report['checked_credentials']==1 and report['credential_decryption_check_passed'] and not report['missing_versions']
    assert not report['memory_reference_table_present'] and not report['key_removal_authorized']
    assert 'ciphertext' not in str(report) and 'fictional-secret' not in str(report)
    missing=Keyring('master-v2',{'master-v2':new.keys['master-v2']})
    broken=await inspect(admin,missing)
    assert broken['missing_versions']==['local-v1'] and broken['failed_credentials']==1
    wrong=Keyring('local-v1',{'local-v1':Fernet.generate_key()})
    bad=await inspect(admin,wrong)
    assert bad['references_available'] and bad['failed_credentials']==1
    unchecked=await inspect(admin,new,False)
    assert unchecked['credential_decryption_check_passed'] is None and unchecked['checked_credentials']==0
    assert [dict(row) for row in admin.rows(t.ai_secrets)]==before
    admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_operator'))
    with pytest.raises(BusinessError):await inspect(admin,new)


@pytest.mark.asyncio
async def test_inventory_does_not_scan_credentials_or_requests_from_another_scope(admin):
    await ready(admin,local=False)
    draft=dict(admin.rows(t.ai_config_drafts)[0]);draft.update(id=uid(),scope_id='another')
    admin.sql(t.ai_config_drafts.insert().values(**draft))
    secret=dict(admin.rows(t.ai_secrets)[0]);secret.update(id=uid(),scope_id='another',origin_draft_id=draft['id'],key_version='foreign-missing')
    admin.sql(t.ai_secrets.insert().values(**secret))
    request=dict(admin.rows(t.ai_config_requests)[0]);request.update(id=uid(),scope_id='another',payload_hash_key_version='foreign-missing')
    admin.sql(t.ai_config_requests.insert().values(**request))
    report=await inspect(admin,admin.keyring)
    assert report['checked_credentials']==1 and report['required_versions']==['local-v1'] and not report['missing_versions']


@pytest.mark.asyncio
async def test_personal_memory_hmac_replay_survives_retained_key_rotation_without_exposing_contents(admin,memory,tmp_path):
    original=await save(admin,memory,text='private stable preference')
    new=staged(admin,tmp_path);memory._keyring=new
    replay=await save(admin,memory,text='private stable preference')
    assert replay==original
    report=await inspect(admin,new)
    old=next(item for item in report['versions'] if item['key_version']=='local-v1')
    assert report['memory_reference_table_present'] and old['memory_replay_requests']==1
    assert 'private stable preference' not in str(report)


@pytest.mark.asyncio
async def test_corrupt_ciphertext_prevents_normal_publication_with_no_partial_release(admin):
    draft=await ready(admin,local=False)
    admin.sql(t.ai_secrets.update().values(ciphertext=b'fictional-invalid-ciphertext'))
    with pytest.raises(BusinessError) as error:await publish(admin,draft)
    assert error.value.code==50303 and admin.count(t.ai_config_releases)==0
    assert admin.rows(t.ai_config_heads)[0]['head_version']==0


async def rotate_and_publish(admin,tmp_path,*,before_rotation=None):
    first=await ready(admin,local=False);old_release,_=await publish(admin,first)
    old=await snapshot(admin);old_proof=admin.proof();old_secret=dict(admin.rows(t.ai_secrets)[0])
    if before_rotation:await before_rotation(old)
    admin.keyring=staged(admin,tmp_path);admin.service._keyring=admin.keyring
    with pytest.raises(BusinessError):verify_proof(admin.keyring,old_proof,admin.actor,1)
    clone,_=await admin.call('create_draft',{'base_release_id':old_release['release_id'],'expected_head_version':1},'rotate-draft')
    operation=profile_op(dict(admin.rows(t.ai_endpoint_policies)[0]),key='second-fictional-key-654321')
    clone,_=await admin.call('patch_draft',clone['id'],{'operations':[operation]},'rotate-profile',clone['etag'],admin.proof())
    validator,_=validation_service(admin);assert (await execute(admin,validator,clone,key='rotate-test'))[0]['status']=='passed'
    await publish(admin,clone,key='rotate-publish')
    unchanged=next(dict(row) for row in admin.rows(t.ai_secrets) if row['id']==old_secret['id'])
    assert unchanged['ciphertext']==old_secret['ciphertext'] and unchanged['key_version']==old_secret['key_version']
    return old,await snapshot(admin),old_secret


@pytest.mark.asyncio
async def test_new_release_uses_new_master_and_old_snapshot_keeps_old_credential(admin,monkeypatch,tmp_path):
    from app.model_config.runtime import model_for
    old,new,old_secret=await rotate_and_publish(admin,tmp_path)
    requests=mock_runtime(admin,monkeypatch,lambda _:httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]}))
    with selected(admin,old):await model_for('chat').ainvoke('old request')
    with selected(admin,new):await model_for('chat').ainvoke('new request')
    assert requests[0].headers['authorization'].endswith('123456') and requests[1].headers['authorization'].endswith('654321')
    new_secret=next(row for row in admin.rows(t.ai_secrets) if row['id']!=old_secret['id'])
    assert new_secret['key_version']=='master-v2' and new_secret['credential_version']==old_secret['credential_version']+1


@pytest.mark.asyncio
async def test_request_already_in_provider_finishes_with_old_key_during_new_master_publication(admin,monkeypatch,tmp_path):
    from app.model_config.runtime import model_for
    entered=asyncio.Event();release=asyncio.Event();task=None
    async def reply(request):
        if request.headers['authorization'].endswith('123456'):
            entered.set();await release.wait()
        return httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]})
    requests=mock_runtime(admin,monkeypatch,reply,patch_keyring_loader=False)
    async def begin(old):
        nonlocal task
        async def read():
            with selected(admin,old):return await model_for('chat').ainvoke('old in-flight request')
        task=asyncio.create_task(read());await asyncio.wait_for(entered.wait(),30)
    try:
        old,new,_=await rotate_and_publish(admin,tmp_path,before_rotation=begin)
        assert task is not None and not task.done()
        with selected(admin,new):assert (await model_for('chat').ainvoke('new request')).content=='OK'
        release.set();assert (await asyncio.wait_for(task,3)).content=='OK'
        assert requests[0].headers['authorization'].endswith('123456') and requests[1].headers['authorization'].endswith('654321')
    finally:
        release.set()
        if task:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_sql_backup_restore_with_correct_keys_preserves_revocation_and_missing_keys_block_restore(admin,tmp_path):
    old,new,old_secret=await rotate_and_publish(admin,tmp_path)
    await revoke(admin,old_secret['id'])
    backup=tmp_path/'restored.sqlite';source=admin.engine.raw_connection();destination=sqlite3.connect(backup)
    try:source.driver_connection.backup(destination)
    finally:destination.close();source.close()
    engine=sa.create_engine(sa.engine.URL.create('sqlite',database=str(backup)))
    restored=SimpleNamespace(engine=engine);control=ConfigService(admin.keyring,owner_id=admin.service.owner_id)
    try:
        with Session(engine) as session:
            view=await inspect_keyring(AsyncFacade(session),admin.actor,admin.keyring,check_decryption=True)
            assert view['credential_decryption_check_passed'] and view['checked_credentials']==2
        with Session(engine) as session:assert (await load_snapshot(AsyncFacade(session))).release_id==new.release_id
        with Session(engine) as session:
            with pytest.raises(BusinessError) as error:
                await PublishingService(control,database_factory(restored)).restore(AsyncFacade(session),admin.actor,
                    old.release_id,{'expected_head_version':2},'restore-revoked',admin.proof())
            assert error.value.code==40926
        control._keyring=Keyring('local-v1',{'local-v1':admin.keyring.keys['local-v1']})
        # Current release needs master-v2, while the caller can still prove
        # identity under the one key retained in this deliberately incomplete file.
        from app.model_config.security import issue_proof
        proof=issue_proof(control.keyring,admin.actor,1)['proof']
        with Session(engine) as session:
            with pytest.raises(BusinessError) as error:
                await PublishingService(control,database_factory(restored)).restore(AsyncFacade(session),admin.actor,
                    new.release_id,{'expected_head_version':2},'restore-missing-key',proof)
            assert error.value.code==50303
        with engine.connect() as connection:
            assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==2
            assert connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_releases)).scalar_one()==2
        missing=tmp_path/'missing-keyring.json'
        with pytest.raises(BusinessError):Keyring.load(missing)
        assert not missing.exists()
    finally:engine.dispose()
