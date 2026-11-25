"""Real SQL transactions for draft CAS, authorization, encryption and replays.

SQLite checks domain logic only. MySQL migration/locking checks have a separate
disposable-server gate; this adapter does not emulate row locks.
"""
from contextlib import asynccontextmanager
import copy
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from cryptography.fernet import Fernet
import httpx
from fastapi import FastAPI
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.contracts import SCHEMA, canonical, empty_payload, validate
from app.model_config.crypto import Keyring
from app.model_config.endpoints import check_policy, canonical_url
from app.model_config.security import Principal, SCOPE, issue_proof, verify_proof
from app.model_config.service import ConfigService, bootstrap_admin, utcnow
from app.models.user import User


class AsyncFacade:
    def __init__(self,session):
        self.session = session
    async def execute(self,statement):
        return self.session.execute(statement)
    async def rollback(self):
        self.session.rollback()
    def get_bind(self):
        return self.session.get_bind()
    @asynccontextmanager
    async def begin(self):
        with self.session.begin():
            yield self


class Store:
    def __init__(self,engine,keyring):
        self.engine,self.keyring = engine,keyring
        self.service = ConfigService(keyring,owner_id='test-manager')
        self.actor = Principal(str(uuid.uuid4()),'access-a',utcnow()+timedelta(hours=1))
        self.other = Principal(str(uuid.uuid4()),'access-b',utcnow()+timedelta(hours=1))
    async def call(self,method,*args,actor=None,**kwargs):
        with Session(self.engine) as session:
            return await getattr(self.service,method)(AsyncFacade(session),actor or self.actor,*args,**kwargs)
    def count(self,table):
        with Session(self.engine) as session:
            return session.scalar(sa.select(sa.func.count()).select_from(table))
    def rows(self,table):
        with Session(self.engine) as session:
            return session.execute(sa.select(table)).mappings().all()
    def sql(self,statement):
        with self.engine.begin() as connection:
            connection.execute(statement)
    def proof(self,actor=None,policy=1):
        return issue_proof(self.keyring,actor or self.actor,policy)['proof']
    async def draft(self,key='draft-1'):
        result,status = await self.call('create_draft',{'base_release_id':None,'expected_head_version':0},key)
        assert status==201
        return result
    async def policy(self,*,local=False,key='endpoint-1', origin=None, allow_cross_service_fallback=False):
        body = {'policy_key':('local' if local else 'cloud') if origin is None else key,
            'origin':origin or ('http://localhost:11434' if local else 'https://models.example.com'),
            'allowed_path_prefix':'/' if local else '/v1',
            'address_mode':'trusted_private' if local else 'public_https',
            'connection_rules':{'allowed_cidrs':['127.0.0.1/32','::1/128']} if local else {},
            'environment':'development'}
        if allow_cross_service_fallback:
            body['connection_rules']['allow_cross_service_fallback'] = True
        result,status = await self.call('create_policy',body,key,self.proof())
        assert status==201
        return result


@pytest.fixture
def store(tmp_path):
    engine = sa.create_engine('sqlite://')
    @sa.event.listens_for(engine,'connect')
    def foreign_keys(connection,_):
        connection.execute('PRAGMA foreign_keys=ON')
    t.metadata.create_all(engine,tables=[User.__table__,*t.CONFIG_TABLES])
    value = Store(engine,Keyring.create(tmp_path/'keyring.json'))
    with engine.begin() as connection:
        for actor,name in ((value.actor,'first'),(value.other,'second')):
            connection.execute(User.__table__.insert().values(uuid=actor.user_id,username=name,
                password='not-used-by-domain-tests',status='active'))
    yield value
    engine.dispose()


@pytest_asyncio.fixture
async def admin(store):
    with Session(store.engine) as session:
        result = await bootstrap_admin(AsyncFacade(session),store.actor.user_id)
        assert result['status']=='bootstrapped'
    return store


def profile(policy,*,local=False):
    return {'display_name':'test','model_kind':'chat','adapter_type':'ollama_native' if local else 'openai_compatible',
        'api_protocol':'ollama_chat' if local else 'chat_completions','vendor':'ollama' if local else 'custom',
        'base_url':policy['origin']+('/' if local else '/v1'),'model':'local-model' if local else 'example-model',
        'model_revision':None,'auth_mode':'none' if local else 'bearer','endpoint_policy_revision_id':policy['id'],
        'context_window':None,'tokenizer_id':None,'tokenizer_revision':None,'token_counting_policy':{},
        'max_output_tokens':256,'connect_timeout_ms':1000,'request_timeout_ms':3000,
        'embedding_dimension':None,'parameters':{'temperature':0.1}}


def prompt_op(role='chat',text='help'):
    return {'op':'replace_prompt','role_key':role,'prompt':{'system_text':text,'template_text':'{question}'}}


def profile_op(policy,*,local=False,key='fictional-secret-long-key-123456'):
    return {'op':'replace_profile','role_key':'chat','profile':profile(policy,local=local),
            'credential':{'action':'keep'} if local else {'action':'replace','api_key':key}}


@pytest.mark.asyncio
async def test_bootstrap_explicit_identity_idempotent_and_no_username_bypass(store):
    with pytest.raises(BusinessError) as error:
        await store.call('summary')
    assert error.value.code==40312
    with Session(store.engine) as session:
        await bootstrap_admin(AsyncFacade(session),store.actor.user_id)
    with Session(store.engine) as session:
        assert (await bootstrap_admin(AsyncFacade(session),store.actor.user_id))['status']=='already_bootstrapped'
    with Session(store.engine) as session:
        with pytest.raises(BusinessError):
            await bootstrap_admin(AsyncFacade(session),store.other.user_id)
    assert store.count(t.ai_config_audits)==1
    summary = await store.call('summary')
    assert summary['effective_for']=='none' and summary['status']=='setup_required'


@pytest.mark.asyncio
async def test_create_cas_replay_before_stale_version_and_expiry(admin):
    draft = await admin.draft()
    replay = await admin.draft()
    assert replay==draft and len(draft['payload']['role_bindings_raw'])==17
    assert admin.count(t.ai_config_requests)==1
    admin.sql(t.ai_config_heads.update().values(head_version=1))
    assert await admin.draft()==draft
    with pytest.raises(BusinessError) as error:
        await admin.draft('different-key')
    assert error.value.code==40920
    admin.sql(t.ai_config_requests.update().values(replay_until=utcnow()-timedelta(seconds=1)))
    with pytest.raises(BusinessError) as error:
        await admin.draft()
    assert error.value.code==40929


@pytest.mark.asyncio
async def test_prompt_versions_etag_and_whole_patch_rollback(admin):
    draft = await admin.draft()
    body = {'operations':[prompt_op('memory_extraction')]}
    updated,_ = await admin.call('patch_draft',draft['id'],body,'patch-1',draft['etag'])
    assert updated['draft_version']==2
    assert updated['prompts'][0]['prompt_key']=='memory_extraction'
    assert (await admin.call('patch_draft',draft['id'],body,'patch-1',draft['etag']))[0]==updated
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],body,'patch-2',draft['etag'])
    assert error.value.code==40921
    binding = copy.deepcopy(draft['payload']['role_bindings_raw']['chat'])
    binding.update(selection='follow_role',source_role='chat')
    bad = {'operations':[prompt_op(),{'op':'replace_binding','role_key':'chat','binding':binding}]}
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],bad,'bad-patch',updated['etag'])
    assert error.value.data['reason']=='ROLE_BINDING_CYCLE'
    assert admin.count(t.ai_prompt_revisions)==1
    assert admin.rows(t.ai_version_counters)[0]['last_num']==1
    assert admin.count(t.ai_config_requests)==2
    assert (await admin.call('get_draft',draft['id']))['etag']==updated['etag']


@pytest.mark.asyncio
async def test_secret_is_encrypted_bound_and_never_in_draft_requests_audit_or_response(admin):
    draft = await admin.draft()
    policy = await admin.policy()
    secret = 'fictional-secret-long-key-123456'
    body = {'operations':[profile_op(policy,key=secret),prompt_op()]}
    updated,_ = await admin.call('patch_draft',draft['id'],body,'patch-secret',draft['etag'],admin.proof())
    row = admin.rows(t.ai_secrets)[0]
    assert secret.encode() not in row['ciphertext']
    identity = {**SCOPE,'id':row['id'],'origin':row['allowed_origin'],'version':row['credential_version']}
    assert admin.keyring.decrypt(row['ciphertext'],row['key_version'],identity)==secret
    with pytest.raises(BusinessError):
        admin.keyring.decrypt(row['ciphertext'],row['key_version'],{**identity,'origin':'https://evil.example'})
    for value in (updated,await admin.call('get_draft',draft['id']),
                  await admin.call('list_rows',t.ai_secrets),admin.rows(t.ai_config_requests),
                  admin.rows(t.ai_config_audits),admin.rows(t.ai_config_drafts)):
        assert secret not in str(value) and 'ciphertext' not in str(value)
    assert updated['credential_states'][0]['has_api_key'] is True
    assert admin.count(t.ai_config_draft_items)==4


@pytest.mark.asyncio
async def test_ollama_no_key_and_http_only_explicit_trusted_policy(admin):
    draft,policy = await admin.draft(),await admin.policy(local=True)
    body = {'operations':[profile_op(policy,local=True)]}
    updated,_ = await admin.call('patch_draft',draft['id'],body,'ollama',draft['etag'])
    assert updated['profiles'][0]['auth_mode']=='none'
    assert not updated['credential_states'][0]['has_api_key']
    assert admin.count(t.ai_secrets)==0


@pytest.mark.asyncio
@pytest.mark.parametrize('mutate,reason',[
    (lambda op:op['profile']['parameters'].update(extra_body={'api_key':'hidden'}),'UNSUPPORTED_MODEL_PARAMETER'),
    (lambda op:op['profile'].update(api_protocol='ollama_chat'),'ADAPTER_PROTOCOL_MISMATCH'),
    (lambda op:op['profile'].update(base_url='https://models.example.com/v11'),'ENDPOINT_DENIED'),
    (lambda op:op['profile'].update(base_url='https://models.example.com/v1/../internal'),'ENDPOINT_DENIED'),
])
async def test_profile_rejects_parameter_and_endpoint_bypasses(admin,mutate,reason):
    draft,policy = await admin.draft(),await admin.policy()
    op = profile_op(policy)
    mutate(op)
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],{'operations':[op]},'bad',draft['etag'],admin.proof())
    assert error.value.data['reason']==reason
    assert admin.count(t.ai_secrets)==0 and admin.count(t.ai_profile_revisions)==0


@pytest.mark.asyncio
async def test_candidate_from_another_draft_denied_and_origin_change_cannot_keep_key(admin):
    first,policy = await admin.draft(),await admin.policy()
    updated,_ = await admin.call('patch_draft',first['id'],{'operations':[profile_op(policy)]},
                                 'first-profile',first['etag'],admin.proof())
    second = await admin.draft('second-draft')
    binding = updated['payload']['role_bindings_raw']['chat']
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',second['id'],{'operations':[{'op':'replace_binding',
            'role_key':'chat','binding':binding}]},'steal',second['etag'])
    assert error.value.http_status==403
    local = await admin.policy(local=True,key='local')
    changed = profile_op(policy)
    changed['profile'].update(base_url=local['origin'],endpoint_policy_revision_id=local['id'])
    changed['credential']={'action':'keep'}
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',first['id'],{'operations':[changed]},'reuse-origin',updated['etag'])
    assert error.value.data['reason']=='CREDENTIAL_ORIGIN_MISMATCH'


@pytest.mark.asyncio
async def test_live_account_grant_and_reader_replay(admin):
    original = await admin.draft()
    admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_reader'))
    assert await admin.draft()==original
    with pytest.raises(BusinessError) as error:
        await admin.draft('new')
    assert error.value.code==40312
    admin.sql(User.__table__.update().where(User.uuid==admin.actor.user_id).values(status='banned'))
    with pytest.raises(BusinessError) as error:
        await admin.draft()
    assert error.value.code==40314


@pytest.mark.asyncio
async def test_reauth_required_bound_to_jti_policy_scope_and_not_body_flag(admin):
    draft = await admin.draft()
    policy = await admin.policy()
    body = {'operations':[profile_op(policy)]}
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],body,'no-proof',draft['etag'])
    assert error.value.code==40110
    for actor,version in ((admin.other,1),(admin.actor,2)):
        with pytest.raises(BusinessError) as error:
            await admin.call('patch_draft',draft['id'],body,'bad-proof',draft['etag'],admin.proof(actor,version))
        assert error.value.code==40111
    with pytest.raises(BusinessError):
        validate('DraftPatch',{**body,'controlled_cli':True})
    changed = Principal(admin.actor.user_id,'new-token',admin.actor.expires_at)
    with pytest.raises(BusinessError) as error:
        verify_proof(admin.keyring,admin.proof(),changed,1)
    assert error.value.code==40111


@pytest.mark.asyncio
async def test_manager_lease_blocks_second_process_and_recovers_after_expiry(admin,monkeypatch):
    await admin.draft()
    admin.service = ConfigService(admin.keyring,owner_id='another-manager')
    with pytest.raises(BusinessError) as error:
        await admin.draft('other')
    assert error.value.code==50304
    admin.sql(t.ai_config_heads.update().values(manager_lease_expires_at=utcnow()-timedelta(seconds=1)))
    await admin.draft('other')
    assert admin.rows(t.ai_config_heads)[0]['manager_generation']==2
    monkeypatch.setenv('UVICORN_WORKERS','2')
    with pytest.raises(BusinessError) as error:
        await admin.draft('multi-worker')
    assert error.value.code==50304


@pytest.mark.asyncio
async def test_idempotency_mismatch_key_rotation_uses_original_hash_version(admin):
    await admin.draft()
    original = admin.service.keyring
    admin.service._keyring = Keyring('new',{**original.keys,'new':Fernet.generate_key()})
    assert (await admin.draft())['draft_version']==1
    with pytest.raises(BusinessError) as error:
        await admin.call('create_draft',{'base_release_id':None,'expected_head_version':1},'draft-1')
    assert error.value.code==40922
    with pytest.raises(BusinessError) as error:
        verify_proof(admin.service.keyring,issue_proof(original,admin.actor,1)['proof'],admin.actor,1)
    assert error.value.code==40111


@pytest.mark.asyncio
async def test_if_match_and_expired_draft_have_no_side_effects(admin):
    draft = await admin.draft()
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],{'operations':[prompt_op()]},'missing',None)
    assert error.value.code==42801 and admin.count(t.ai_prompt_revisions)==0
    admin.sql(t.ai_config_drafts.update().values(expires_at=utcnow()-timedelta(seconds=1)))
    with pytest.raises(BusinessError) as error:
        await admin.call('get_draft',draft['id'])
    assert error.value.http_status==410


@pytest.mark.parametrize('url',[
    'http://user:pass@localhost:11434','https://example.com/v1?key=secret',
    'https://example.com/%2e%2e','https://example.com/a/../b','file:///etc/passwd',
    'https://example.com\\@evil.com','http://localhost:99999','http://localhost:0',
])
def test_url_parser_rejects_ambiguous_or_credential_urls(url):
    with pytest.raises(BusinessError):
        canonical_url(url)


@pytest.mark.parametrize('cidr',['0.0.0.0/0','169.254.0.0/16','100.100.100.200/32','::/0','224.0.0.0/4'])
def test_private_policy_cannot_include_metadata_or_global_ranges(cidr):
    with pytest.raises(BusinessError):
        check_policy({'origin':'http://localhost:11434','allowed_path_prefix':'/',
            'address_mode':'trusted_private','connection_rules':{'allowed_cidrs':[cidr]}})


def test_keyring_never_regenerates_or_overwrites_and_is_immutable(tmp_path):
    path = tmp_path/'keyring.json'
    with pytest.raises(BusinessError):
        Keyring.load(path)
    assert not path.exists()
    keyring = Keyring.create(path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        Keyring.create(path)
    assert path.read_bytes()==original
    with pytest.raises(TypeError):
        keyring.keys['evil']=Fernet.generate_key()
    assert 'redacted' in repr(keyring)


def test_packaged_schema_and_true_jcs_numeric_and_utf16_ordering():
    document = json.loads(Path('docs/升级方案附件/05_配置契约.schema.json').read_text(encoding='utf-8'))
    assert document==SCHEMA
    assert canonical({'number':1.0})==b'{"number":1}'
    assert canonical({'\uffff':1,'\U0001f600':2}).decode()=='{"\U0001f600":2,"\uffff":1}'
    validate('DraftPatch',{'operations':[prompt_op('memory_extraction')]})


@pytest_asyncio.fixture
async def api(admin,monkeypatch):
    from app.model_config import router as module
    from app.utils import auth_utils
    from app.core.failed_response_register import register_exception_handlers
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(module.router,prefix='/api/v1')
    application.include_router(module.auth_router,prefix='/api/v1')
    application.state.redis = SimpleNamespace(incr=AsyncMock(return_value=1),expire=AsyncMock(),
                                               eval=AsyncMock(return_value=1))
    async def database():
        with Session(admin.engine) as session:
            yield AsyncFacade(session)
    async def unused_auth_database():
        yield None
    application.dependency_overrides[module.config_db] = database
    # Keep the real JWT verification and principal construction in the request.
    application.dependency_overrides[auth_utils.get_db] = unused_auth_database
    monkeypatch.setattr(auth_utils,'JWT_SECRET','isolated-test-signing-key-not-for-real-deployments')
    monkeypatch.setattr(auth_utils,'is_token_blacklisted',AsyncMock(return_value=False))
    monkeypatch.setattr(module,'service',admin.service)
    headers = {'Authorization':'Bearer '+auth_utils.create_access_token(admin.actor.user_id,
                                                                       {'jti':admin.actor.access_jti})}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://test',
                                headers=headers) as client:
        yield client,application


@pytest.mark.asyncio
async def test_http_real_jwt_etag_conflict_error_envelope_and_no_fake_publish(api):
    client,_ = api
    roles = await client.get('/api/v1/model-config/roles')
    assert roles.status_code==200 and len(roles.json()['data']['items'])==18
    assert roles.headers['Cache-Control']=='no-store'
    created = await client.post('/api/v1/model-config/drafts',json={
        'base_release_id':None,'expected_head_version':0},headers={'Idempotency-Key':'http-create'})
    assert created.status_code==201
    view = created.json()['data']
    assert created.headers['etag']==view['etag']
    response = await client.patch('/api/v1/model-config/drafts/'+view['id'],
        json={'operations':[prompt_op()]},headers={'Idempotency-Key':'http-patch','If-Match':'wrong'})
    assert response.status_code==409 and response.json()['code']==40921
    assert response.json()['request_id']==response.headers['X-Request-ID']
    assert (await client.post('/api/v1/model-config/drafts/'+view['id']+'/publish',json={})).status_code==422


@pytest.mark.asyncio
async def test_http_unauthorized_banned_blacklisted_and_schema_redaction(api,admin,monkeypatch):
    from app.utils import auth_utils
    client,_ = api
    response = await client.get('/api/v1/model-config',headers={'Authorization':''})
    assert response.status_code==401
    invalid = await client.post('/api/v1/model-config/drafts',json={
        'base_release_id':None,'expected_head_version':0,'api_key':'do-not-echo-me'},
        headers={'Idempotency-Key':'http-invalid'})
    assert invalid.status_code==422 and 'do-not-echo-me' not in invalid.text
    monkeypatch.setattr(auth_utils,'is_token_blacklisted',AsyncMock(return_value=True))
    assert (await client.get('/api/v1/model-config')).status_code==401
    monkeypatch.setattr(auth_utils,'is_token_blacklisted',AsyncMock(return_value=False))
    admin.sql(User.__table__.update().where(User.uuid==admin.actor.user_id).values(status='banned'))
    assert (await client.get('/api/v1/model-config')).status_code==403


@pytest.mark.asyncio
async def test_http_reauth_password_failure_rate_limit_and_bound_proof(api,admin,monkeypatch):
    from app.utils import auth_utils
    client,application = api
    monkeypatch.setattr(auth_utils,'check_login_attempts',AsyncMock())
    monkeypatch.setattr(auth_utils,'record_login_failure',AsyncMock())
    monkeypatch.setattr(auth_utils,'clear_login_attempts',AsyncMock())
    monkeypatch.setattr(auth_utils,'verify_password',lambda *_:False)
    failed = await client.post('/api/v1/auth/reauth',json={'password':'private-password'})
    assert failed.status_code==401 and failed.json()['code']==40103
    assert 'private-password' not in failed.text
    auth_utils.record_login_failure.assert_awaited_once()
    monkeypatch.setattr(auth_utils,'verify_password',lambda *_:True)
    success = await client.post('/api/v1/auth/reauth',json={'password':'private-password'})
    assert success.status_code==200 and success.headers['Cache-Control']=='no-store'
    verify_proof(admin.keyring,success.json()['data']['proof'],admin.actor,1)
    application.state.redis.eval.return_value = 6
    assert (await client.post('/api/v1/auth/reauth',json={'password':'private-password'})).status_code==429


@pytest.mark.asyncio
async def test_revoked_credential_readable_but_cannot_be_resaved_as_usable(admin):
    draft,policy = await admin.draft(),await admin.policy()
    view,_ = await admin.call('patch_draft',draft['id'],{'operations':[profile_op(policy)]},
                              'secret',draft['etag'],admin.proof())
    admin.sql(t.ai_secrets.update().values(status='revoked',revoked_at=utcnow(),revocation_epoch=1))
    read = await admin.call('get_draft',draft['id'])
    assert read['profiles'] and read['credential_states'][0]['has_api_key']
    updated,_=await admin.call('patch_draft',draft['id'],{'operations':[prompt_op()]},'edit-revoked',view['etag'])
    assert 'CREDENTIAL_REVOKED' in updated['publication_blockers']
    assert updated['credential_states'][0]['status']=='disabled' or updated['credential_states'][0]['status']=='revoked'
    # Editing does not make the credential usable: a profile save keeping it
    # still fails, and publication/probes retain their independent strict checks.
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],{'operations':[profile_op(policy,key='unused')|{'credential':{'action':'keep'}}]},
            'keep-revoked',updated['etag'])
    assert error.value.code==40926


@pytest.mark.asyncio
async def test_grant_policy_cas_last_admin_and_immediate_proof_invalidation(admin):
    with pytest.raises(BusinessError) as error:
        await admin.call('change_grant',admin.actor.user_id,{'expected_policy_version':1},
                         'last-admin',admin.proof(),revoke=True)
    assert error.value.code==40928
    body = {'config_role_key':'config_admin','expected_policy_version':1}
    result,_ = await admin.call('change_grant',admin.other.user_id,body,'add-admin',admin.proof())
    assert result['policy_version']==2
    # A successful grant change revokes every proof bound to the old policy.
    with pytest.raises(BusinessError) as error:
        await admin.call('change_grant',admin.actor.user_id,{'expected_policy_version':2},
                         'old-proof',admin.proof(),revoke=True)
    assert error.value.code==40111
    with pytest.raises(BusinessError) as error:
        await admin.call('change_grant',admin.actor.user_id,{'expected_policy_version':1},
                         'stale-policy',admin.proof(policy=2),revoke=True)
    assert error.value.code==40920
    await admin.call('change_grant',admin.actor.user_id,{'config_role_key':'config_reader',
        'expected_policy_version':2},'demote',admin.proof(policy=2))
    assert (await admin.call('summary'))['config_role_key']=='config_reader'
    with pytest.raises(BusinessError) as error:
        await admin.draft()
    assert error.value.code==40312
    assert (await admin.call('summary',actor=admin.other))['config_role_key']=='config_admin'


@pytest.mark.asyncio
async def test_operator_can_edit_but_cannot_manage_endpoints_or_grants(admin):
    await admin.call('change_grant',admin.other.user_id,{'config_role_key':'config_operator',
        'expected_policy_version':1},'operator',admin.proof())
    await admin.call('create_draft',{'base_release_id':None,'expected_head_version':0},'operator-draft',actor=admin.other)
    with pytest.raises(BusinessError) as error:
        await admin.call('change_grant',admin.actor.user_id,{'expected_policy_version':2},
            'operator-revoke',admin.proof(admin.other,2),actor=admin.other,revoke=True)
    assert error.value.code==40312


@pytest.mark.asyncio
async def test_draft_pagination_and_abandon_replay(admin):
    first,second = await admin.draft(),await admin.draft('second')
    page = await admin.call('list_rows',t.ai_config_drafts,limit=1)
    tail = await admin.call('list_rows',t.ai_config_drafts,limit=1,cursor=page['next_cursor'])
    assert {page['items'][0]['id'],tail['items'][0]['id']}=={first['id'],second['id']}
    original,_ = await admin.call('abandon_draft',first['id'],'abandon',first['etag'])
    assert original['status']=='abandoned'
    assert (await admin.call('abandon_draft',first['id'],'abandon',first['etag']))[0]==original
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',first['id'],{'operations':[prompt_op()]},'abandoned-edit',first['etag'])
    assert error.value.code==40923


@pytest.mark.asyncio
async def test_follow_role_uses_own_prompt_and_dynamic_synthesize_has_no_fixed_key(admin):
    draft,policy = await admin.draft(),await admin.policy(local=True)
    view,_ = await admin.call('patch_draft',draft['id'],{'operations':[
        profile_op(policy,local=True),prompt_op(),prompt_op('session_title'),prompt_op('synthesize')]},
        'prepare',draft['etag'])
    bindings = copy.deepcopy(view['payload']['role_bindings_raw'])
    bindings['chat'].update(enabled=True,failure_mode='error')
    bindings['session_title'].update(enabled=True,selection='follow_role',source_role='chat',failure_mode='default_title')
    bindings['synthesize'].update(enabled=True,selection='follow_execution',failure_mode='error')
    final,_ = await admin.call('patch_draft',draft['id'],{'operations':[
        {'op':'replace_binding','role_key':role,'binding':bindings[role]}
        for role in ('chat','session_title','synthesize')]},'bind',view['etag'])
    chat = final['resolved_static_bindings']['chat']
    title = final['resolved_static_bindings']['session_title']
    assert chat['profile_revision_id']==title['profile_revision_id']
    assert chat['prompt_revision_id']!=title['prompt_revision_id']
    assert 'synthesize' not in final['resolved_static_bindings']
    assert final['follow_execution_roles']==['synthesize']
    dynamic = next(row for row in final['credential_states'] if row['role_key']=='synthesize')
    assert dynamic['status']=='dynamic' and dynamic['has_api_key'] is None


@pytest.mark.asyncio
async def test_login_lock_cannot_be_bypassed_by_old_proof_but_completed_replay_is_safe(admin):
    draft,policy = await admin.draft(),await admin.policy()
    operation = {'operations':[profile_op(policy)]}
    proof = admin.proof()
    view,_ = await admin.call('patch_draft',draft['id'],operation,'before-lock',draft['etag'],proof)
    admin.service.reauth_guard = AsyncMock(side_effect=BusinessError(40104,http_status=403))
    assert (await admin.call('patch_draft',draft['id'],operation,'before-lock',draft['etag'],proof))[0]==view
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],operation,'after-lock',view['etag'],proof)
    assert error.value.code==40104


@pytest.mark.asyncio
async def test_intermediate_candidates_remain_in_inventory(admin):
    draft = await admin.draft()
    await admin.call('patch_draft',draft['id'],{'operations':[prompt_op(text='first'),prompt_op(text='second')]},
                     'two-prompts',draft['etag'])
    assert admin.count(t.ai_prompt_revisions)==2
    assert admin.count(t.ai_config_draft_items)==2


def test_reauth_expiry_and_signature_are_checked(store):
    from jose import jwt
    proof = store.proof()
    claims = jwt.get_unverified_claims(proof)
    claims.update(iat=claims['iat']-700,exp=claims['exp']-700)
    expired = jwt.encode(claims,store.keyring.purpose_key('reauth'),algorithm='HS256',headers={'kid':'local-v1'})
    with pytest.raises(BusinessError) as error:
        verify_proof(store.keyring,expired,store.actor,1)
    assert error.value.code==40111
    tampered = jwt.encode(jwt.get_unverified_claims(proof),b'wrong-key',algorithm='HS256',headers={'kid':'local-v1'})
    with pytest.raises(BusinessError):
        verify_proof(store.keyring,tampered,store.actor,1)


@pytest.mark.asyncio
async def test_commit_failure_never_returns_success_or_leaves_partial_versions(admin,monkeypatch):
    from sqlalchemy.exc import OperationalError
    # Session.begin() invokes the transaction object directly, so inject a
    # commit failure at the engine transaction event, not an API-level mock.
    def reject_commit(connection):
        raise OperationalError('COMMIT',None,RuntimeError('isolated commit failure'))
    sa.event.listen(admin.engine,'commit',reject_commit)
    try:
        with pytest.raises(OperationalError):
            await admin.draft()
    finally:
        sa.event.remove(admin.engine,'commit',reject_commit)
    assert admin.count(t.ai_config_drafts)==0
    assert admin.count(t.ai_config_requests)==0


@pytest.mark.asyncio
async def test_http_control_store_failure_is_structured_and_does_not_expose_bound_values(api,monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.model_config import router as module
    client,application = api
    @asynccontextmanager
    async def unavailable():
        raise OperationalError('query',{'api_key':'do-not-leak-database-params'},RuntimeError('offline'))
        yield
    # Restore the real dependency so its SQL exception boundary is exercised.
    application.dependency_overrides.pop(module.config_db)
    monkeypatch.setattr(module,'async_session_factory',unavailable)
    response = await client.get('/api/v1/model-config')
    assert response.status_code==503 and response.json()['code']==50303
    assert 'do-not-leak-database-params' not in response.text


@pytest.mark.asyncio
async def test_request_deadline_rolls_back_candidate_changes(admin,monkeypatch):
    draft = await admin.draft()
    start = utcnow()
    calls = 0
    async def advancing(db):
        nonlocal calls
        calls += 1
        # The post-handler deadline check must include work/lock time.
        return start+timedelta(seconds=31 if calls>=7 else 0)
    monkeypatch.setattr(admin.service,'_now',advancing)
    with pytest.raises(BusinessError) as error:
        await admin.call('patch_draft',draft['id'],{'operations':[prompt_op()]},'late',draft['etag'])
    assert error.value.code==50410
    assert admin.count(t.ai_prompt_revisions)==0 and admin.count(t.ai_config_requests)==1
