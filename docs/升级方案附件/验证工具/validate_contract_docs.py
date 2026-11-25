"""Validate the v1.4 document set and JSON Schema without importing application code."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    ann = root / 'docs/升级方案附件'
    schema = json.loads((ann/'05_配置契约.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    errors = []
    cases = []
    def check(name, definition, data, expected=True):
        validator = Draft202012Validator({'$ref':'#/$defs/'+definition,'$defs':schema['$defs']},
                                        format_checker=FormatChecker())
        actual = validator.is_valid(data)
        cases.append({'case':name,'definition':definition,'expected':expected,'actual':actual})
        if actual != expected:
            errors.append('schema case mismatch: '+name)

    roles = schema['$defs']['RoleKey']['enum']
    empty = {'enabled':False,'selection':'explicit','profile_revision_id':None,
             'credential_revision_id':None,'prompt_revision_id':None,'source_role':None,
             'fallback':[],'failure_mode':'disabled'}
    payload = {'schema_version':2,'role_bindings_raw':{key:copy.deepcopy(empty) for key in roles},
               'index_bindings':{}}
    payload['role_bindings_raw']['chat'].update(enabled=True,profile_revision_id='profile-chat',
        credential_revision_id='credential-chat',prompt_revision_id='prompt-main',failure_mode='error')
    check('complete_17_roles','DraftPayload',payload)
    missing = copy.deepcopy(payload); missing['role_bindings_raw'].pop('planner')
    check('missing_role','DraftPayload',missing,False)
    bad = copy.deepcopy(payload); bad['role_bindings_raw']['chat']['profile_revision_id']=None
    check('enabled_explicit_requires_profile','DraftPayload',bad,False)
    follow = copy.deepcopy(payload)
    follow['role_bindings_raw']['synthesize'].update(enabled=True,selection='follow_execution',
        prompt_revision_id='prompt-synthesis',failure_mode='error')
    check('dynamic_synthesis','DraftPayload',follow)
    bad = copy.deepcopy(follow); bad['role_bindings_raw']['planner']=copy.deepcopy(bad['role_bindings_raw']['synthesize'])
    check('dynamic_only_for_synthesis','DraftPayload',bad,False)
    bad = copy.deepcopy(payload); bad['role_bindings_raw']['embedding']['prompt_revision_id']='forbidden'
    check('embedding_has_no_prompt','DraftPayload',bad,False)
    bad = copy.deepcopy(payload); bad['role_bindings_raw']['chat']['failure_mode']='allow_anything'
    check('closed_failure_mode','DraftPayload',bad,False)
    check('first_draft','DraftCreate',{'base_release_id':None,'expected_head_version':0})
    check('wrong_head_type','DraftCreate',{'base_release_id':None,'expected_head_version':'0'},False)
    check('credential_replace','CredentialAction',{'action':'replace','api_key':'fictional-test-key'})
    check('keep_cannot_carry_key','CredentialAction',{'action':'keep','api_key':'fictional-test-key'},False)
    check('empty_replace','CredentialAction',{'action':'replace','api_key':''},False)
    check('credential_revoke_reason','CredentialRevocationInput',{'reason':'suspected_leak'})
    check('credential_revoke_missing_reason','CredentialRevocationInput',{},False)
    check('credential_revoke_no_key','CredentialRevocationInput',{'reason':'suspected_leak','api_key':'fictional-test-key'},False)
    check('credential_revoke_empty_reason','CredentialRevocationInput',{'reason':''},False)
    revoke={'credential_revision_id':'credential-test','credential_version':1,'status':'revoked',
        'revocation_epoch':1,'dispatch_blocked':True,'cancel_requested':True,'already_revoked':False}
    check('credential_revoke_receipt','CredentialRevocationResult',revoke)
    check('credential_revoke_no_remote_promise','CredentialRevocationResult',dict(revoke,remote_cancelled=True),False)
    check('credential_revoke_requires_dispatch_block','CredentialRevocationResult',dict(revoke,dispatch_blocked=False),False)
    check('credential_revoke_legacy_epoch','CredentialRevocationResult',dict(revoke,revocation_epoch=0,already_revoked=True))
    redrive={'expected_attempts':5,'reason':'operator_retry'}
    check('outbox_redrive_input','OutboxRedriveInput',redrive)
    check('outbox_redrive_missing_attempts','OutboxRedriveInput',{'reason':'operator_retry'},False)
    check('outbox_redrive_excess_budget','OutboxRedriveInput',dict(redrive,expected_attempts=6),False)
    check('outbox_redrive_no_free_text','OutboxRedriveInput',dict(redrive,reason='secret text'),False)
    check('outbox_redrive_no_payload','OutboxRedriveInput',dict(redrive,payload={}),False)
    receipt={'source_event_id':'source','event_id':'child','status':'pending','attempts':0,'original_attempts':5,
             'delivery_guarantee':'at_least_once','config_head_changed':False}
    check('outbox_redrive_receipt','OutboxRedriveResult',receipt)
    check('outbox_redrive_no_success_promise','OutboxRedriveResult',dict(receipt,status='succeeded'),False)
    check('outbox_redrive_no_head_move','OutboxRedriveResult',dict(receipt,config_head_changed=True),False)
    check('empty_patch','DraftPatch',{'operations':[]},False)
    check('prompt_patch','DraftPatch',{'operations':[{'op':'replace_prompt','role_key':'chat',
        'prompt':{'system_text':'test','template_text':'{question}'}}]})
    check('memory_extraction_prompt_patch','DraftPatch',{'operations':[{'op':'replace_prompt',
        'role_key':'memory_extraction','prompt':{'system_text':'test','template_text':'{question}'}}]})
    check('grant_update','GrantUpdateInput',{'config_role_key':'config_operator','expected_policy_version':1})
    check('grant_update_closed_role','GrantUpdateInput',{'config_role_key':'chat','expected_policy_version':1},False)
    check('grant_update_forbids_cli_bypass','GrantUpdateInput',{'config_role_key':'config_admin',
        'expected_policy_version':1,'controlled_cli':True},False)
    check('grant_delete','GrantDeleteInput',{'expected_policy_version':1})
    check('grant_delete_requires_cas','GrantDeleteInput',{},False)
    check('draft_test_standard','DraftTestInput',{'role_key':'chat','expected_draft_version':2})
    check('draft_test_fallback_role','DraftTestInput',{'role_key':'chat','expected_draft_version':2,'fallback_role_key':'session_title'})
    check('draft_test_closed_fallback_role','DraftTestInput',{'role_key':'chat','expected_draft_version':2,'fallback_role_key':'https://backup.example'},False)
    check('draft_test_local_cold','DraftTestInput',{'role_key':'embedding','expected_draft_version':2,'budget':'local_cold'})
    check('draft_test_requires_version','DraftTestInput',{'role_key':'chat'},False)
    check('draft_test_closed_role','DraftTestInput',{'role_key':'not_registered','expected_draft_version':2},False)
    check('draft_test_cannot_raise_call_budget','DraftTestInput',{'role_key':'chat','expected_draft_version':2,'max_calls':1000},False)
    check('draft_test_closed_budget','DraftTestInput',{'role_key':'chat','expected_draft_version':2,'budget':'unlimited'},False)
    check('prompt_patch_is_full_object','DraftPatch',{'operations':[{'op':'replace_prompt','role_key':'chat',
        'prompt':{'system_text':'test'}}]},False)
    check('unknown_operation','DraftPatch',{'operations':[{'op':'merge_arbitrarily','role_key':'chat'}]},False)
    check('rebase_choice','RebaseInput',{'expected_head_version':2,
        'resolutions':[{'path':'/role_bindings_raw/chat','choice':'ours'}]})
    check('rebase_unknown_choice','RebaseInput',{'expected_head_version':2,
        'resolutions':[{'path':'/role_bindings_raw/chat','choice':'unsafe'}]},False)
    check('publish_versions','PublishInput',{'expected_draft_version':1,'expected_head_version':0})
    check('publish_missing_version','PublishInput',{'expected_head_version':0},False)
    profile={'display_name':'fixture','model_kind':'chat','adapter_type':'openai_compatible',
        'api_protocol':'chat_completions','vendor':'custom','base_url':'https://example.invalid/v1',
        'model':'fixture-model','model_revision':None,'auth_mode':'bearer',
        'endpoint_policy_revision_id':'policy-fixture','context_window':32768,'tokenizer_id':None,
        'tokenizer_revision':None,'token_counting_policy':{},'max_output_tokens':256,
        'connect_timeout_ms':1000,'request_timeout_ms':10000,'embedding_dimension':None,'parameters':{}}
    check('profile_real_field_names','ProfileInput',profile)
    bad=copy.deepcopy(profile);bad['parameters']['api_key']='must-not-persist-here'
    check('profile_parameter_cannot_override_key','ProfileInput',bad,False)
    bad=copy.deepcopy(profile);bad['parameters']['max_retries']=100
    check('profile_parameter_cannot_override_retry','ProfileInput',bad,False)
    native=copy.deepcopy(profile);native.update(model_kind='embedding',adapter_type='dashscope_native',
        api_protocol='dashscope_embed',vendor='dashscope',auth_mode='dashscope_key',embedding_dimension=1024)
    check('native_dashscope_profile','ProfileInput',native)
    bad=copy.deepcopy(profile);bad['timeouts']={}
    check('legacy_field_alias_rejected','ProfileInput',bad,False)
    patch={'operations':[{'op':'replace_profile','role_key':'embedding','profile':native,
        'credential':{'action':'keep'},'index_keys':[]}]}
    check('explicit_embedding_targets','DraftPatch',patch)
    bad=copy.deepcopy(patch);bad['operations'][0].pop('index_keys')
    check('embedding_targets_required','DraftPatch',bad,False)
    static={key:{'enabled':value['enabled'],'profile_revision_id':value['profile_revision_id'],
        'credential_revision_id':value['credential_revision_id'],
        'credential_version':3 if value['credential_revision_id'] else None,
        'prompt_revision_id':value['prompt_revision_id'],'fallback':value['fallback'],
        'failure_mode':value['failure_mode']} for key,value in payload['role_bindings_raw'].items()
        if key!='synthesize'}
    check('release_raw_static_dynamic','ReleaseManifest',{'schema_version':2,
        'role_bindings_raw':follow['role_bindings_raw'],'resolved_static_bindings':static,
        'follow_execution_roles':['synthesize'],'index_bindings':{}})
    check('reauth_output','ReauthOutput',{'proof':'fixture-proof','proof_type':'config_reauth',
        'expires_in':600,'expires_at':'2026-11-15T13:00:00Z','actor_id':'actor-fixture',
        'scope_type':'system','scope_id':'system'})
    policy={'policy_key':'fixture','origin':'https://example.invalid','allowed_path_prefix':'/v1',
        'address_mode':'public_https','connection_rules':{},'environment':'test'}
    check('endpoint_policy_fields','EndpointPolicyInput',policy)
    bad=copy.deepcopy(policy);bad.pop('policy_key')
    check('endpoint_policy_key_required','EndpointPolicyInput',bad,False)
    state={'role_key':'synthesize','has_api_key':None,'masked_suffix':None,'credential_revision_id':None,
           'credential_version':None,'status':'dynamic','auth_mode':None}
    check('credential_state_is_per_role','RoleCredentialState',state)
    bad=copy.deepcopy(state);bad['api_key']='must-not-return'
    check('read_dto_rejects_secret','RoleCredentialState',bad,False)
    check('error_compatible_envelope','ErrorEnvelope',{'code':40920,'message':'conflict','detail':None,
        'data':{'reason':'HEAD_VERSION_CONFLICT'},'request_id':'0123456789abcdef0123456789abcdef'})
    check('error_missing_detail','ErrorEnvelope',{'code':40920,'message':'conflict',
        'data':{},'request_id':'0123456789abcdef0123456789abcdef'},False)

    # Historical immutable releases remain readable; new drafts require the new registry.
    legacy=copy.deepcopy(payload);legacy['schema_version']=1
    legacy['role_bindings_raw'].pop('memory_extraction')
    legacy_static=copy.deepcopy(static);legacy_static.pop('memory_extraction',None)
    legacy_release={'schema_version':1,'role_bindings_raw':legacy['role_bindings_raw'],
        'resolved_static_bindings':legacy_static,'follow_execution_roles':[],'index_bindings':{}}
    check('legacy_v1_release_is_readable','ReleaseManifestRead',legacy_release)
    check('legacy_draft_requires_explicit_upgrade','DraftPayload',legacy,False)
    bad=copy.deepcopy(payload);bad['role_bindings_raw'].pop('memory_extraction')
    check('v2_requires_memory_role_even_disabled','DraftPayload',bad,False)
    bad=copy.deepcopy(legacy_release);bad['role_bindings_raw']['memory_extraction']=copy.deepcopy(empty)
    check('v1_release_cannot_be_mutated_to_17_roles','ReleaseManifestRead',bad,False)
    expansion='rag_query_expand'
    v3=copy.deepcopy(payload)
    v3['role_bindings_raw'][expansion]=copy.deepcopy(empty)
    check('v2_rejects_expansion','DraftPayload',v3,False)
    v3['schema_version']=3
    check('complete_v3_18_roles','DraftPayload',v3)
    check('v3_requires_expansion','DraftPayload',{**payload,'schema_version':3},False)
    for name,fields in [('follow',{'selection':'follow_role','source_role':'chat'}),('backup',{'fallback':['chat']})]:
        bad=copy.deepcopy(v3);bad['role_bindings_raw'][expansion].update(fields)
        check('expansion_rejects_'+name,'DraftPayload',bad,False)
    release3={**v3,'resolved_static_bindings':{**static, expansion:static['memory_extraction']},'follow_execution_roles':[]}
    check('v3_release_is_readable','ReleaseManifestRead',release3)
    check('v3_requires_compiled_expansion','ReleaseManifestRead',{**release3,'resolved_static_bindings':static},False)
    check('memory_save','MemorySaveInput',{'content_md':'# 回答偏好\n默认中文'})
    check('memory_owner_not_client_supplied','MemorySaveInput',{'content_md':'x','user_id':'other'},False)
    check('memory_character_limit','MemorySaveInput',{'content_md':'x'*32769},False)
    check('memory_settings_disable','MemorySettingsInput',{'enabled':False})
    check('memory_settings_not_empty','MemorySettingsInput',{},False)
    check('memory_mode_closed','MemorySettingsInput',{'management_mode':'overwrite_manual'},False)
    check('memory_clear_explicit','MemoryClearInput',{'purge_history':True})
    check('memory_clear_unknown_field','MemoryClearInput',{'purge_history':True,'path':'../../x'},False)
    check('memory_empty_command','MemoryEmptyInput',{})
    check('memory_command_rejects_model_override','MemoryEmptyInput',{'api_key':'x'},False)
    check('memory_off_one_turn','MemoryRequestOption',{'memory_mode':'off'})
    check('memory_mode_unknown','MemoryRequestOption',{'memory_mode':'forced'},False)
    check('memory_pagination_limit','MemoryPagination',{'limit':101},False)
    check('memory_extraction_no_change','MemoryExtractionResult',{'decision':'no_change','content_md':None,'evidence':[]})
    extraction={'decision':'update','content_md':'# 回答偏好\n中文',
        'evidence':[{'source_message_id':1,'quote':'默认用中文'}]}
    check('memory_extraction_evidence','MemoryExtractionResult',extraction)
    bad=copy.deepcopy(extraction);bad['evidence']=[]
    check('memory_extraction_requires_evidence','MemoryExtractionResult',bad,False)
    bad=copy.deepcopy(extraction);bad['role']='system'
    check('memory_extraction_no_role_escalation','MemoryExtractionResult',bad,False)
    absent={'exists':False,'filename':'memory.md','revision_id':None,'revision_number':0,
        'head_version':0,'memory_epoch':0,'content_md':None,'content_hash':None,
        'settings':{'enabled':True,'auto_extract':True,'management_mode':'auto'},
        'extraction_available':False,'updated_at':None}
    check('memory_absent_read','MemoryDocument',absent)
    bad=copy.deepcopy(absent);bad['exists']=True
    check('memory_existing_requires_revision','MemoryDocument',bad,False)
    bad=copy.deepcopy(absent);bad['filename']='../other.md'
    check('memory_fixed_logical_filename','MemoryDocument',bad,False)

    main_doc=root/'docs/企业级Agent与模型配置热更新升级方案_2026-11-15.md'
    response=root/'docs/企业级Agent与模型配置热更新升级方案_审查处理记录_2026-11-15.md'
    docs=[main_doc,response,*sorted(ann.glob('*.md'))]
    acceptance=(ann/'04_里程碑验收矩阵.md').read_text(encoding='utf-8')
    ids=re.findall(r'^\| (M[0-4]-(?:R|M)?\d+) \|',acceptance,re.M)
    if len(ids)!=len(set(ids)):errors.append('duplicate acceptance IDs')
    if len(ids)!=105:errors.append('expected 105 acceptance cases')
    for p in docs:
        text=p.read_text(encoding='utf-8')
        if len(re.findall(r'^```',text,re.M))%2:errors.append(p.name+': unbalanced fences')
        for link in re.findall(r'\]\(([^)]+)\)',text):
            if link.startswith(('http://','https://')):continue
            path,_,anchor=link.partition('#')
            target=(p.parent/path).resolve() if path else p
            if not target.exists():errors.append(p.name+': missing link '+link)
            elif re.fullmatch(r's\d+',anchor) and ('id="'+anchor+'"') not in target.read_text(encoding='utf-8'):
                errors.append(p.name+': missing anchor '+anchor)
        for block in re.findall(r'```json\s*\n(.*?)\n```',text,re.S):json.loads(block)
        for case_id in re.findall(r'\bM[0-4]-(?:R|M)?\d+\b',text):
            if case_id not in ids:errors.append(p.name+': unknown acceptance ID '+case_id)
        if any(re.search(r'[ \t]+$',line) for line in text.splitlines()):errors.append(p.name+': trailing whitespace')
    for line in acceptance.splitlines():
        if re.match(r'^\| M[0-4]-(?:R|M)?\d+ \|',line) and len(line.split('|'))!=8:
            errors.append('acceptance row must have six fields: '+line[:30])
    ddl=(ann/'02_数据模型DDL.sql').read_text(encoding='utf-8')
    for name,body in re.findall(r'CREATE TABLE (\w+) \((.*?)\) ENGINE=InnoDB',ddl,re.S):
        if 'scope_type ' not in body or 'scope_id ' not in body:errors.append('missing scope: '+name)
    forbidden=[r'\bworkflow_steps\b',r'\bworkflow_events\b',r'\bembedding_profile_revision\b',
               r'\bquery_profile_revision\b',r'M1-R02/R17']
    for p in [main_doc,ann/'01_角色注册表.md',ann/'03_API契约.md']:
        for pattern in forbidden:
            if re.search(pattern,p.read_text(encoding='utf-8')):errors.append(p.name+': obsolete alias '+pattern)
    second=re.findall(r'^\| (R2-(?:P0|P1|O|D|V)-\d+) \|',response.read_text(encoding='utf-8'),re.M)
    if len(second)!=41 or len(set(second))!=41:errors.append('second-round coverage is not 41 unique items')
    report={'schema_cases':len(cases),'schema_case_failures':sum(x['actual']!=x['expected'] for x in cases),
            'roles':len(roles),'v3_roles':len(schema['$defs']['RoleKeyV3']['enum']),
            'acceptance_cases':len(ids),'second_review_items':len(second),
            'document_count':len(docs),'cases':cases,'errors':errors,'passed':not errors}
    output=root/'.cache'/'plan-contract-v14'/'result.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='cases'},ensure_ascii=False,indent=2))
    if errors:raise SystemExit(1)


if __name__=='__main__':
    main()
