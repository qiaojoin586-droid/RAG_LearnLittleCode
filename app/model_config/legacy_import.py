"""Offline legacy migration plan and controlled CLI-only import into a draft.

No SDK clients, provider requests, endpoint grants, publication or index writes.
The plan holds credentials only in memory; repr, preview and request/audit rows
never contain those values. This module is deliberately absent from HTTP routes.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import ROLE_KEYS, canonical, content_hash, empty_payload, validate
from app.model_config.endpoints import canonical_url, check_profile
from app.model_config.errors import fail
from app.model_config.prompt_contracts import default_template, prompt_metadata, validate_prompt
from app.model_config.registry import PROMPT_KEYS, ROLE_NAMES
from app.model_config.security import SCOPE, authorize, scoped
from app.model_config.service import etag, uid, utcnow

CLOUD = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
DEFAULT_REQUEST_KEY = 'legacy-import:system:system:v1'


@dataclass(repr=False)
class LegacyPlan:
    profiles: dict
    credentials: dict
    prompts: dict
    enabled: dict
    warnings: list[str]

    def __repr__(self):return '<LegacyPlan redacted>'

    def preview(self):
        return {'source_kind':'legacy_environment','importer_version':1,'target':'reviewable_draft',
            'required_origins':sorted({canonical_url(p['base_url'])[0] for p in self.profiles.values()}),
            'roles':[{'role_key':role,'enabled':self.enabled[role],
                'model':self.profiles.get(role,{}).get('model'),
                'base_url':self.profiles.get(role,{}).get('base_url'),
                'adapter_type':self.profiles.get(role,{}).get('adapter_type'),
                'has_api_key':bool(self.credentials.get(role))} for role in ROLE_KEYS],
            'warnings':list(self.warnings)}


def build_plan(env, agent=None, *, prompt_loader=default_template, ppt_timeout=45):
    """Read only supported legacy options. Unknown providers fail closed."""
    agent=agent or {}
    provider=env.get('MODEL_PROVIDER','dashscope').strip().lower()
    if provider not in ('dashscope','ollama'):fail(40002,'LEGACY_PROVIDER_UNSUPPORTED',422)
    native=provider=='ollama'
    key=env.get('DASHSCOPE_API_KEY','')
    base=env.get('OLLAMA_BASE_URL','http://localhost:11434') if native else CLOUD
    warnings=['此为迁移草稿；启用角色均需重新测试，核对提示词与输出限制后才能发布。',
        '向量角色保持停用：旧索引缺少向量空间基线证据。',
        '个人记忆提取保持停用：旧环境中没有对应的独立配置。',
        '未知上下文窗口保持为空；YAML 的 Token 预算不是模型能力证明。',
        '未声明的输出上限保持为空，当前运行时会采用默认上限；请审核。']
    if native:warnings.append('PPT 大纲仍按旧代码使用 DashScope；Ollama 的思考能力需独立测试。')
    if env.get('DASHSCOPE_BASE_URL'):warnings.append('旧工厂使用固定 DashScope 地址；DASHSCOPE_BASE_URL 未参与迁移。')
    def number(name,default):
        try:
            result=float(env.get(name,str(default)))
            if not 0<result<=300:raise ValueError
            return round(result*1000)
        except (ValueError,TypeError,OverflowError):fail(40002,'LEGACY_SETTING_INVALID',422,field=name)
    def on(name):return env.get(name,'true').strip().lower()=='true'
    def make(model,*,cloud=False,timeout=None,embedding=False):
        local=native and not cloud
        profile={'display_name':'legacy','model_kind':'embedding' if embedding else 'chat',
            'adapter_type':'ollama_native' if local else 'dashscope_native' if embedding else 'openai_compatible',
            'api_protocol':('ollama_embed' if local else 'dashscope_embed') if embedding else ('ollama_chat' if local else 'chat_completions'),
            'vendor':'ollama' if local else 'dashscope','base_url':base if local else ('https://dashscope.aliyuncs.com' if embedding else CLOUD),
            'model':model,'model_revision':None,'auth_mode':'none' if local else 'dashscope_key' if embedding else 'bearer',
            'endpoint_policy_revision_id':'00000000-0000-0000-0000-000000000000',
            'context_window':None,'tokenizer_id':None,'tokenizer_revision':None,
            'token_counting_policy':{'mode':'conservative'},'max_output_tokens':None,
            'connect_timeout_ms':min(10000,timeout or 120000),
            'request_timeout_ms':timeout or 120000,'embedding_dimension':None,'parameters':{}}
        validate('ProfileInput',profile);canonical_url(profile['base_url'])
        return profile
    main=make(env.get('OLLAMA_CHAT_MODEL','qwen3:latest') if native else env.get('DASHSCOPE_CHAT_MODEL','qwen3-max'),
        timeout=number('OLLAMA_TIMEOUT',30) if native else 120000)
    classifier=make(env.get('CLASSIFIER_MODEL','qwen3:0.6b' if native else 'qwen3-flash'),timeout=10000 if native else 30000)
    planner=make(env.get('PLAN_MODEL','qwen3:0.6b' if native else 'qwen3-flash'),timeout=15000 if native else 30000)
    profiles={role:copy.deepcopy(main) for role in ROLE_KEYS if role not in ('memory_extraction','synthesize')}
    profiles.update(classifier=classifier,planner=planner,session_title=copy.deepcopy(classifier))
    profiles['reflection']=copy.deepcopy(planner if agent.get('reflection',{}).get('critic_model','light')=='light' else main)
    profiles['vision']=make((env.get('OLLAMA_VISION_MODEL','').strip() or 'qwen3-vl') if native else (env.get('VISION_MODEL','').strip() or 'unconfigured'),
        timeout=number('OLLAMA_TIMEOUT',30) if native else 120000)
    profiles['autocomplete']=make(env.get('AUTOCOMPLETE_MODEL','qwen3:0.6b' if native else 'qwen3.7-flash'),timeout=number('AUTOCOMPLETE_TIMEOUT',5))
    if not native:
        try:profiles['autocomplete']['max_output_tokens']=int(env.get('AUTOCOMPLETE_MAX_TOKENS','200'))
        except (ValueError,TypeError):fail(40002,'LEGACY_SETTING_INVALID',422,field='AUTOCOMPLETE_MAX_TOKENS')
    profiles['ppt_outline']=make(env.get('DASHSCOPE_CHAT_MODEL','qwen3-max'),cloud=True,timeout=round((ppt_timeout+5)*1000))
    profiles['embedding']=make(env.get('OLLAMA_EMBED_MODEL','nomic-embed-text') if native else env.get('DASHSCOPE_EMBED_MODEL','text-embedding-v3'),embedding=True)
    enabled={role:True for role in ROLE_KEYS}
    enabled.update(embedding=False,memory_extraction=False,autocomplete=on('AUTOCOMPLETE_ENABLED'),auto_tag=on('AUTO_TAG_ENABLED'),
        classifier=bool(agent.get('classifier',{}).get('llm_enabled',True)),
        reflection=bool(agent.get('reflection',{}).get('enable',True)),
        rag_summarize=bool(agent.get('rag',{}).get('enable_summarize',False)))
    if not native and not env.get('VISION_MODEL','').strip():profiles.pop('vision');enabled['vision']=False
    credentials={}
    for role,profile in list(profiles.items()):
        profile['display_name']=ROLE_NAMES[role]+'（旧配置）'
        validate('ProfileInput',profile)
        check_profile(profile,{'origin':canonical_url(profile['base_url'])[0],'allowed_path_prefix':'/','status':'active'})
        if profile['auth_mode']!='none':
            if key:credentials[role]=key
            else:
                profiles.pop(role);enabled[role]=False
    enabled['synthesize']=enabled['chat']
    if not key:warnings.append('未读取到 DashScope Key：依赖该 Key 的角色保持停用，不创建虚构凭据。')
    prompts={role:validate('PromptInput',{'system_text':'','template_text':prompt_loader(role)})
             for role in ROLE_KEYS if role!='embedding'}
    for role,prompt in prompts.items():validate_prompt(role,prompt)
    return LegacyPlan(profiles,credentials,prompts,enabled,warnings)


@dataclass(frozen=True)
class _CLIActor:
    user_id: str
    expires_at: object
    access_jti: None = None


class LegacyImporter:
    def __init__(self,control):self.control=control

    async def apply(self,db,user_id,plan,policies,request_key=DEFAULT_REQUEST_KEY):
        # Local process authority is explicit and separate from HTTP/JWT/reauth.
        # _mutate still locks and verifies a live active administrator grant.
        actor=_CLIActor(user_id,utcnow()+timedelta(seconds=30))
        body={'importer_version':1,'profiles':plan.profiles,'credentials':plan.credentials,
              'prompts':plan.prompts,'enabled':plan.enabled,'endpoint_policies':policies}
        if len(canonical(body))>1048576:fail(40002,'IMPORT_TOO_LARGE',422)
        async def operation(head):
            await authorize(db,actor,'config.publish')
            now=await self.control._now(db)
            payload=empty_payload()
            draft=dict(id=uid(),**SCOPE,base_release_id=head['active_release_id'],base_head_version=head['head_version'],
                draft_version=1,payload=payload,payload_hash=content_hash(payload),status='editing',
                expires_at=now+timedelta(days=7),created_by=user_id,created_at=now,updated_at=now)
            await db.execute(t.ai_config_drafts.insert().values(**draft))
            for role in ROLE_KEYS:
                binding=payload['role_bindings_raw'][role]
                if role in plan.profiles:
                    profile=copy.deepcopy(plan.profiles[role]);origin=canonical_url(profile['base_url'])[0]
                    if origin not in policies:fail(40002,'IMPORT_ENDPOINT_POLICY_REQUIRED',422,origin=origin)
                    profile['endpoint_policy_revision_id']=policies[origin];validate('ProfileInput',profile)
                    credential={'action':'replace','api_key':plan.credentials[role]} if role in plan.credentials else {'action':'keep'}
                    validate('CredentialAction',credential)
                    row=await self.control._profile(db,actor,draft,role,profile,credential)
                    binding.update(profile_revision_id=row['id'],credential_revision_id=row['credential_revision_id'])
                if role in plan.prompts:
                    prompt=validate('PromptInput',plan.prompts[role]);validate_prompt(role,prompt)
                    row=dict(id=uid(),**SCOPE,prompt_key=PROMPT_KEYS[role],revision=await self.control._counter(db,'prompt',PROMPT_KEYS[role]),
                        **prompt,**prompt_metadata(role),content_hash=content_hash(prompt),source='import',status='candidate',
                        source_draft_id=draft['id'],created_by=user_id,created_at=now)
                    await db.execute(t.ai_prompt_revisions.insert().values(**row))
                    await self.control._inventory(db,draft['id'],'prompt',row['id'])
                    binding['prompt_revision_id']=row['id']
                binding.update(enabled=plan.enabled[role],failure_mode='error' if plan.enabled[role] else 'disabled')
                if role=='synthesize':binding.update(selection='follow_execution')
            validate('DraftPayload',payload)
            await self.control._view(db,draft,register=True)
            await db.execute(t.ai_config_drafts.update().where(scoped(t.ai_config_drafts),t.ai_config_drafts.c.id==draft['id'])
                .values(payload=payload,payload_hash=content_hash(payload)))
            await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),t.ai_config_requests.c.id==head['request_record_id'])
                .values(execution_state={'authority_kind':'controlled_cli','importer_version':1}))
            result={'draft_id':draft['id'],'draft_version':1,'etag':etag(draft),'status':'editing',
                'base_head_version':head['head_version'],'requires_review':True,'published':False}
            return result,draft['id'],{'draft_id':draft['id'],'authority_kind':'controlled_cli','importer_version':1}
        return await self.control._mutate(db,actor,'bootstrap.import','config.publish','legacy-import',body,request_key,operation,http_status=201)
