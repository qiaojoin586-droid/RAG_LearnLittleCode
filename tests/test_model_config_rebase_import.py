"""Migration/rebase boundaries with real SQL transactions and fixed HTTP probes."""
import copy
import json
import builtins
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.contracts import empty_payload
from app.model_config.legacy_import import LegacyImporter,build_plan
from app.model_config.rebase import RebaseService,merge
from test_model_config_control_plane import admin,store,AsyncFacade,prompt_op  # noqa: F401
from test_model_config_publication import publish,ready
from test_model_config_validation import validation_service,execute,fixture_handler


async def rebase(admin,draft,head=1,resolutions=None,key='rebase'):
    with Session(admin.engine) as session:
        return await RebaseService(admin.service).rebase(AsyncFacade(session),admin.actor,draft['id'],
            {'expected_head_version':head,'resolutions':resolutions or []},key,draft['etag'])


async def patch(admin,draft,role,text,key):
    return (await admin.call('patch_draft',draft['id'],{'operations':[prompt_op(role,text)]},key,draft['etag']))[0]


@pytest.mark.asyncio
async def test_disjoint_roles_merge_and_replay_before_old_etag(admin):
    ours=await admin.draft('ours');theirs=await admin.draft('theirs')
    ours=await patch(admin,ours,'chat','local','local')
    theirs=await patch(admin,theirs,'session_title','remote','remote')
    await publish(admin,theirs)
    result,status=await rebase(admin,ours)
    assert status==200 and result['draft_version']==3 and result['base_head_version']==1
    assert {p['system_text'] for p in result['prompts']}=={'local','remote'}
    assert (await rebase(admin,ours))[0]==result
    assert admin.count(t.ai_config_releases)==admin.count(t.outbox_events)==1
    with pytest.raises(BusinessError) as error:await rebase(admin,ours,key='stale')
    assert error.value.code==40921


@pytest.mark.asyncio
@pytest.mark.parametrize('choice,text',[('ours','local'),('theirs','remote')])
async def test_conflict_is_atomic_explicit_choice_and_redacted(admin,choice,text):
    ours=await patch(admin,await admin.draft('ours'),'chat','local','local')
    theirs=await patch(admin,await admin.draft('theirs'),'chat','remote','remote')
    await publish(admin,theirs)
    count=admin.count(t.ai_config_requests)
    with pytest.raises(BusinessError) as error:await rebase(admin,ours)
    assert error.value.code==40925
    assert error.value.data['conflicts']==['/role_bindings_raw/chat']
    assert 'system_text' not in json.dumps(error.value.data)
    assert admin.count(t.ai_config_requests)==count
    assert (await admin.call('get_draft',ours['id']))['etag']==ours['etag']
    result,_=await rebase(admin,ours,resolutions=[{'path':'/role_bindings_raw/chat','choice':choice}])
    assert [p['system_text'] for p in result['prompts']]==[text]
    with pytest.raises(BusinessError) as changed:
        await rebase(admin,ours,resolutions=[{'path':'/role_bindings_raw/chat','choice':'theirs' if choice=='ours' else 'ours'}])
    assert changed.value.code==40922


def test_index_deletion_vs_change_conflicts_and_explicit_delete():
    base=empty_payload();base['index_bindings']['notes']={'id':'old'}
    ours=copy.deepcopy(base);ours['index_bindings'].pop('notes')
    theirs=copy.deepcopy(base);theirs['index_bindings']['notes']={'id':'new'}
    _,conflicts=merge(base,ours,theirs)
    assert conflicts==['/index_bindings/notes']
    result,conflicts=merge(base,ours,theirs,[{'path':conflicts[0],'choice':'ours'}])
    assert not conflicts and result['index_bindings']=={}
    with pytest.raises(BusinessError):merge(base,base,base,[{'path':'/index_bindings/notes','choice':'theirs'}])


@pytest.mark.asyncio
async def test_rebase_fences_running_tests_and_rejects_prior_proofs(admin):
    current=await ready(admin)
    merged=[]
    async def reply(request):
        if not merged:merged.append((await rebase(admin,current,head=0))[0])
        return fixture_handler(request)
    validator,requests=validation_service(admin,reply)
    cancelled,status=await execute(admin,validator,current,key='pending')
    assert status==200 and cancelled['status']=='cancelled' and len(requests)==1
    assert (await execute(admin,validator,current,key='pending'))[0]==cancelled
    result=merged[0]
    assert result['validation_coverage']==[]
    with pytest.raises(BusinessError) as error:await publish(admin,result)
    assert error.value.data['reason']=='CAPABILITY_UNVERIFIED'
    assert admin.count(t.ai_config_releases)==0
    fresh,_=validation_service(admin)
    assert (await execute(admin,fresh,result,key='fresh'))[0]['status']=='passed'
    assert (await publish(admin,result))[1]==200


@pytest.mark.asyncio
async def test_head_and_authority_failures_do_not_change_draft(admin):
    draft=await admin.draft()
    with pytest.raises(BusinessError) as error:await rebase(admin,draft,head=3)
    assert error.value.code==40920
    admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_reader'))
    with pytest.raises(BusinessError) as error:await rebase(admin,draft,head=0)
    assert error.value.code==40312
    assert admin.count(t.ai_config_audits)==2  # bootstrap + draft


def test_native_legacy_mapping_preserves_separate_roles_and_yaml_switches():
    plan=build_plan({'MODEL_PROVIDER':'ollama','OLLAMA_CHAT_MODEL':'primary','CLASSIFIER_MODEL':'classifier',
        'PLAN_MODEL':'planner','AUTOCOMPLETE_ENABLED':'false'},
        {'classifier':{'llm_enabled':False},'reflection':{'critic_model':'main','enable':False},'rag':{'enable_summarize':True}})
    assert plan.profiles['chat']['api_protocol']=='ollama_chat'
    assert plan.profiles['classifier']['model']=='classifier' and plan.profiles['planner']['model']=='planner'
    assert plan.profiles['session_title']['model']=='classifier' and plan.profiles['reflection']['model']=='primary'
    assert not plan.enabled['classifier'] and not plan.enabled['autocomplete'] and not plan.enabled['reflection']
    assert plan.enabled['rag_summarize'] and not plan.enabled['embedding'] and not plan.enabled['memory_extraction']
    assert plan.profiles['embedding']['embedding_dimension'] is None
    assert plan.preview()['required_origins']==['http://localhost:11434']


@pytest.mark.parametrize('env',[
    {'MODEL_PROVIDER':'typo'},{'MODEL_PROVIDER':'ollama','OLLAMA_BASE_URL':'http://user:secret@localhost:11434'},
    {'AUTOCOMPLETE_TIMEOUT':'nan'},{'AUTOCOMPLETE_MAX_TOKENS':'secret-not-an-int'},
])
def test_invalid_source_rejected_without_echoing_secret(env):
    with pytest.raises(BusinessError) as error:build_plan(env)
    assert 'secret' not in str(error.value.data) and 'secret' not in error.value.message


async def apply(admin,plan,policies,key=None,actor=None):
    with Session(admin.engine) as session:
        args=[AsyncFacade(session),actor or admin.actor.user_id,plan,policies]
        return await LegacyImporter(admin.service).apply(*args,**({'request_key':key} if key else {}))


@pytest.mark.asyncio
async def test_cli_import_encryption_idempotency_ownership_audit_and_no_publication(admin):
    policy=(await admin.call('create_policy',{'policy_key':'cloud','origin':'https://dashscope.aliyuncs.com',
        'allowed_path_prefix':'/','address_mode':'public_https','connection_rules':{},'environment':'development'},
        'policy',admin.proof()))[0]
    secret='fictional-key-import-do-not-log'
    plan=build_plan({'DASHSCOPE_API_KEY':secret,'VISION_MODEL':'vision'})
    assert secret not in repr(plan) and secret not in json.dumps(plan.preview())
    result,status=await apply(admin,plan,{policy['origin']:policy['id']})
    assert status==201 and not result['published'] and result['requires_review']
    assert (await apply(admin,plan,{policy['origin']:policy['id']}))[0]==result
    view=await admin.call('get_draft',result['draft_id'])
    assert len(view['payload']['role_bindings_raw'])==17 and view['validation_coverage']==[]
    assert not view['binding_issues'] and not view['payload']['index_bindings']
    assert admin.count(t.ai_config_releases)==admin.count(t.outbox_events)==admin.count(t.ai_config_validations)==0
    secrets=admin.rows(t.ai_secrets)
    assert len(secrets)>10 and len({s['id'] for s in secrets})==len(secrets)
    for row in secrets:
        assert row['origin_draft_id']==result['draft_id'] and secret.encode() not in row['ciphertext']
        assert admin.keyring.decrypt(row['ciphertext'],row['key_version'],{**{k:row[k] for k in ('scope_type','scope_id','id')},
            'origin':row['allowed_origin'],'version':row['credential_version']})==secret
    for table in (t.ai_config_requests,t.ai_config_audits):
        assert secret not in str(admin.rows(table)) and 'template_text' not in str(admin.rows(table))
    request=next(r for r in admin.rows(t.ai_config_requests) if r['action']=='bootstrap.import')
    assert request['actor_access_jti'] is None and request['reauth_verified_at'] is None
    assert request['execution_state']['authority_kind']=='controlled_cli'
    plan.credentials['chat']='different-fictional-key'
    with pytest.raises(BusinessError) as error:await apply(admin,plan,{policy['origin']:policy['id']})
    assert error.value.code==40922
    assert admin.count(t.ai_config_drafts)==1


@pytest.mark.asyncio
async def test_missing_policy_or_permission_rolls_back_all_candidates(admin):
    plan=build_plan({'MODEL_PROVIDER':'ollama'})
    with pytest.raises(BusinessError) as error:await apply(admin,plan,{})
    assert error.value.data['reason']=='IMPORT_ENDPOINT_POLICY_REQUIRED'
    assert admin.count(t.ai_config_drafts)==admin.count(t.ai_prompt_revisions)==admin.count(t.ai_version_counters)==0
    with pytest.raises(BusinessError) as error:await apply(admin,plan,{},actor=admin.other.user_id)
    assert error.value.code==40312


@pytest.mark.asyncio
async def test_import_after_existing_head_only_creates_new_draft(admin):
    live=await ready(admin);published,_=await publish(admin,live)
    plan=build_plan({'MODEL_PROVIDER':'ollama'})
    policy=admin.rows(t.ai_endpoint_policies)[0]
    imported,_=await apply(admin,plan,{policy['origin']:policy['id']})
    assert imported['base_head_version']==1
    assert admin.rows(t.ai_config_heads)[0]['active_release_id']==published['release_id']
    assert admin.count(t.ai_config_releases)==admin.count(t.outbox_events)==1


@pytest.mark.asyncio
async def test_import_missing_second_origin_rolls_back_partial_native_candidates(admin):
    local=await admin.policy(local=True)
    plan=build_plan({'MODEL_PROVIDER':'ollama','DASHSCOPE_API_KEY':'fictional-ppt-secret'})
    with pytest.raises(BusinessError) as error:await apply(admin,plan,{local['origin']:local['id']})
    assert error.value.data['origin']=='https://dashscope.aliyuncs.com'
    for table in (t.ai_config_drafts,t.ai_profile_revisions,t.ai_prompt_revisions,t.ai_secrets,t.ai_version_counters):
        if table is t.ai_version_counters:continue  # pre-existing endpoint policy counter
        assert admin.count(table)==0
    assert admin.count(t.ai_config_requests)==1  # only the original endpoint operation


def test_cli_preview_never_imports_database_or_provider_factory(monkeypatch,tmp_path,capsys):
    from app.model_config import cli
    agent=tmp_path/'agent.yaml';agent.write_text('classifier:\n  llm_enabled: false\n',encoding='utf-8')
    ppt=tmp_path/'ppt.yaml';ppt.write_text('ppt:\n  outline_timeout: 17\n',encoding='utf-8')
    monkeypatch.setenv('MODEL_PROVIDER','ollama');monkeypatch.setenv('DASHSCOPE_API_KEY','fictional-preview-secret')
    monkeypatch.setenv('OLLAMA_BASE_URL','http://localhost:11434')
    monkeypatch.setattr(sys,'argv',['cli','preview-legacy','--agent-config',str(agent),'--ppt-config',str(ppt)])
    original=builtins.__import__
    def guard(name,*args,**kwargs):
        if name in ('app.db.database','app.utils.factory'):raise AssertionError('Offline preview attempted runtime import')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guard)
    cli.main()
    output=capsys.readouterr().out
    assert 'fictional-preview-secret' not in output
    preview=json.loads(output)
    assert preview['target']=='reviewable_draft' and preview['roles'][2]['enabled'] is False
