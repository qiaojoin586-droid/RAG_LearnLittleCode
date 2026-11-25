"""Three-way merge of atomic role/index bindings; no field-level key mixing."""
from __future__ import annotations

import copy
import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import empty_payload,upgrade_payload,validate,content_hash
from app.model_config.errors import fail
from app.model_config.security import scoped


def merge(base,ours,theirs,resolutions=()):
    choices={}
    for item in resolutions:
        if item['path'] in choices:fail(40002,'DUPLICATE_REBASE_RESOLUTION',422)
        choices[item['path']]=item['choice']
    result=empty_payload();missing=object();conflicts=[];used=set()
    for group in ('role_bindings_raw','index_bindings'):
        for key in sorted(set(base[group])|set(ours[group])|set(theirs[group])):
            before=base[group].get(key,missing);local=ours[group].get(key,missing);remote=theirs[group].get(key,missing)
            path=f'/{group}/{key}'
            if local==remote or remote==before:value=local
            elif local==before:value=remote
            else:
                conflicts.append(path)
                if path not in choices:continue
                used.add(path);value=local if choices[path]=='ours' else remote
            if value is missing:result[group].pop(key,None)
            else:result[group][key]=copy.deepcopy(value)
    if set(choices)-used:fail(40002,'INVALID_REBASE_RESOLUTION',422)
    if 'rag_query_expand' in result['role_bindings_raw']:
        result['schema_version'] = 3
    return result,[path for path in conflicts if path not in choices]


class RebaseService:
    def __init__(self,control):self.control=control

    async def _payload(self,db,release_id):
        if not release_id:return empty_payload()
        release=await self.control._entity(db,t.ai_config_releases,release_id)
        return upgrade_payload({key:release[key] for key in ('schema_version','role_bindings_raw','index_bindings')})

    async def _comparison(self,db,draft,path,values):
        group,key=path.split('/')[1:]
        async def summary(payload):
            binding=payload[group].get(key)
            if binding is None:return None
            if group=='index_bindings':return dict(binding)
            result={name:binding.get(name) for name in ('enabled','selection','source_role','failure_mode',
                'profile_revision_id','prompt_revision_id','credential_revision_id')}
            if binding.get('profile_revision_id'):
                profile=await self.control._entity(db,t.ai_profile_revisions,binding['profile_revision_id'],draft['id'],allow_invalid=True)
                result.update(model=profile['model'],model_revision=profile['revision'])
            if binding.get('prompt_revision_id'):
                prompt=await self.control._entity(db,t.ai_prompt_revisions,binding['prompt_revision_id'],draft['id'],allow_invalid=True)
                result['prompt_revision']=prompt['revision']
            return result
        return {'path':path,**{name:await summary(payload) for name,payload in values.items()}}

    async def rebase(self,db,actor,draft_id,body,key,match):
        validate('RebaseInput',body)
        async def operation(head):
            if body['expected_head_version']!=head['head_version']:
                fail(40920,'HEAD_VERSION_CONFLICT',actual=head['head_version'])
            draft=await self.control._draft(db,draft_id,edit=True,match=match)
            base=await self._payload(db,draft['base_release_id'])
            theirs=await self._payload(db,head['active_release_id'])
            ours=upgrade_payload(draft['payload'])
            payload,conflicts=merge(base,ours,theirs,body.get('resolutions',[]))
            if conflicts:
                comparisons=[await self._comparison(db,draft,path,{'base':base,'ours':ours,'theirs':theirs}) for path in conflicts]
                fail(40925,'REBASE_CONFLICT',conflicts=conflicts,comparisons=comparisons,
                    head_version=head['head_version'],draft_version=draft['draft_version'])
            validate('DraftPayload',payload)
            now=await self.control._now(db)
            draft.update(payload=payload,payload_hash=content_hash(payload),base_release_id=head['active_release_id'],
                base_head_version=head['head_version'],draft_version=draft['draft_version']+1,status='editing',updated_at=now)
            view=await self.control._view(db,draft,register=True)
            await db.execute(t.ai_config_drafts.update().where(scoped(t.ai_config_drafts),t.ai_config_drafts.c.id==draft_id)
                .values(**{name:draft[name] for name in ('payload','payload_hash','base_release_id','base_head_version',
                                                        'draft_version','status','updated_at')}))
            # Preserve passed rows as immutable historical evidence. Running work
            # is fenced immediately, and its idempotent result becomes terminal.
            from app.model_config.validation_service import ValidationService
            running=(await db.execute(sa.select(t.ai_config_validations).where(scoped(t.ai_config_validations),
                t.ai_config_validations.c.draft_id==draft_id,t.ai_config_validations.c.status.in_(['pending','running'])).with_for_update())).mappings().all()
            for item in running:
                row=dict(item);row.update(status='cancelled',result={'status':'cancelled','reason':'DRAFT_REBASED','usage':None},valid_until=None)
                await db.execute(t.ai_config_validations.update().where(scoped(t.ai_config_validations),t.ai_config_validations.c.id==row['id'])
                    .values(status='cancelled',result=row['result'],valid_until=None))
                await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),t.ai_config_requests.c.id==row['request_record_id'])
                    .values(status='succeeded',response_http_status=200,response_json=ValidationService.view(row),completed_at=now))
            return view,draft_id,{'draft_id':draft_id,'draft_version':draft['draft_version'],
                'base_head_version':head['head_version'],'resolved_paths':[item['path'] for item in body.get('resolutions',[])]}
        return await self.control._mutate(db,actor,'draft.rebase','config.edit',draft_id,body,key,operation,match=match)
