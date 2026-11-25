"""Owner-scoped Markdown versions, CAS and body-free persistent idempotency."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import json
import re

import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.model_config.contracts import canonical,validate
from app.model_config.crypto import Keyring
from app.model_config.security import scoped
from app.model_config.service import ensure_row,uid,utcnow,json_view
from app.model_config import tables as config
from app.models.user import User
from app.user_memory import tables as t

TEMPLATE='# 个人记忆\n\n## 稳定偏好\n\n## 长期背景\n\n## 需要持续关注的事项\n'
SENSITIVE=re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~-]{15,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|api[_-]?key\s*[:=]\s*[\"\']?[^\s\"\']{8,})',re.I)


def error(code,reason,status=409,**data):
    raise BusinessError(code,message={40940:'记忆文件已更新，请核对后再保存',40941:'建议已过期或基线已变化',
        40942:'已有记忆生成任务进行中',40943:'幂等键已用于其他操作内容',40944:'个人记忆提取角色尚不可用',
        40440:'未找到记忆内容',41040:'该版本已被彻底清理',41340:'记忆正文超过 32768 字节',
        42240:'记忆内容不符合安全要求',42241:'没有可用于提取的成功对话来源',50340:'个人记忆存储暂不可用',
        42801:'缺少 If-Match 版本',40314:'账户不可用',40929:'请求重放期限已过',
    }.get(code,'个人记忆操作未完成'),http_status=status,data={'reason':reason,**data})


def normalize(content):
    result=content.replace('\r\n','\n').replace('\r','\n')
    try:encoded=result.encode('utf-8')
    except UnicodeError:error(42240,'MEMORY_CONTENT_REJECTED',422,field='content_md')
    if len(encoded)>32768:error(41340,'MEMORY_CONTENT_TOO_LARGE',413,limit_bytes=32768)
    if SENSITIVE.search(result) or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',result):
        error(42240,'MEMORY_CONTENT_REJECTED',422,field='content_md')
    return result


def etag(version):return f'"memory-{version}"'


class MemoryService:
    def __init__(self,keyring=None):self._keyring=keyring
    @property
    def keyring(self):
        if self._keyring is None:self._keyring=Keyring.load()
        return self._keyring
    def fingerprint(self,payload,version=None):
        try:return hmac.new(self.keyring.purpose_key('memory-idempotency',version),canonical(payload),hashlib.sha256).hexdigest()
        except BusinessError:error(50340,'MEMORY_STORE_UNAVAILABLE',503)

    async def authorize(self,db,user_id,*,lock=False):
        query=sa.select(User.status).where(User.uuid==user_id)
        status=(await db.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
        if status!='active':error(40314,'ACCOUNT_INACTIVE',403)

    async def head(self,db,user_id,*,lock=False,create=False):
        table=t.user_memory_heads
        if create:
            await ensure_row(db,table,{'user_id':user_id,'last_revision_number':0,'head_version':0,'memory_epoch':0,
                'enabled':True,'auto_extract':True,'management_mode':'auto','created_at':utcnow(),'updated_at':utcnow()})
        query=sa.select(table).where(table.c.user_id==user_id)
        row=(await db.execute(query.with_for_update() if lock else query)).mappings().first()
        return dict(row) if row else {'user_id':user_id,'active_revision_id':None,'head_version':0,
            'last_revision_number':0,'memory_epoch':0,'enabled':True,'auto_extract':True,'management_mode':'auto','updated_at':None}

    async def revision(self,db,user_id,revision_id,*,purged=False):
        row=(await db.execute(sa.select(t.user_memory_revisions).where(t.user_memory_revisions.c.user_id==user_id,
            t.user_memory_revisions.c.id==revision_id))).mappings().first()
        if not row:error(41040 if purged else 40440,'MEMORY_RESULT_PURGED' if purged else 'MEMORY_NOT_FOUND',410 if purged else 404)
        return dict(row)

    async def availability(self,db,release_id=None):
        head=release_id or (await db.execute(sa.select(config.ai_config_heads.c.active_release_id).where(scoped(config.ai_config_heads)))).scalar_one_or_none()
        if not head:return False,None
        raw=(await db.execute(sa.select(config.ai_config_releases.c.role_bindings_raw).where(
            scoped(config.ai_config_releases),config.ai_config_releases.c.id==head))).scalar_one()
        return bool(raw.get('memory_extraction',{}).get('enabled')),head

    async def document(self,db,user_id,head=None,*,purged=False):
        head=head or await self.head(db,user_id)
        revision=await self.revision(db,user_id,head['active_revision_id'],purged=purged) if head['active_revision_id'] else None
        available,_=await self.availability(db)
        value={'exists':revision is not None,'filename':'memory.md','revision_id':revision['id'] if revision else None,
            'revision_number':revision['revision_number'] if revision else 0,'head_version':head['head_version'],
            'memory_epoch':head['memory_epoch'],'content_md':revision['content_md'] if revision else None,
            'content_hash':revision['content_hash'] if revision else None,
            'settings':{key:head[key] for key in ('enabled','auto_extract','management_mode')},
            'extraction_available':available,'updated_at':json_view(head['updated_at'])}
        return validate('MemoryDocument',value)

    async def get(self,db,user_id):
        await self.authorize(db,user_id)
        return await self.document(db,user_id)

    async def new_revision(self,db,head,content,source,source_request_id=None,*,parent=True):
        content=normalize(content)
        row=dict(id=uid(),user_id=head['user_id'],revision_number=head['last_revision_number']+1,
            content_md=content,content_hash=hashlib.sha256(content.encode()).hexdigest(),source_kind=source,
            parent_revision_id=head['active_revision_id'] if parent else None,source_request_id=source_request_id,created_at=utcnow())
        await db.execute(t.user_memory_revisions.insert().values(**row))
        head.update(active_revision_id=row['id'],last_revision_number=row['revision_number'],
                    head_version=head['head_version']+1,updated_at=utcnow())
        await self.update_head(db,head)
        return row

    async def update_head(self,db,head):
        await db.execute(t.user_memory_heads.update().where(t.user_memory_heads.c.user_id==head['user_id']).values(
            **{key:head[key] for key in ('active_revision_id','last_revision_number','head_version','memory_epoch',
                                       'enabled','auto_extract','management_mode','updated_at')}))

    async def snapshot(self,db,user_id,*,off=False,budget=2048):
        if off:return None
        async with db.begin():
            await self.authorize(db,user_id,lock=True)
            head=await self.head(db,user_id,lock=True,create=True)
            if not head['enabled']:return None
            if not head['active_revision_id']:await self.new_revision(db,head,TEMPLATE,'auto_init')
            revision=await self.revision(db,user_id,head['active_revision_id'])
            if budget<512:return None
            # Byte bound is conservative for byte-level tokenizers; never split
            # a UTF-8 codepoint and prefer a paragraph boundary when possible.
            content=revision['content_md'];encoded=content.encode('utf-8')
            shortened=encoded[:1536].decode('utf-8',errors='ignore')
            if len(encoded)>1536 and '\n\n' in shortened:shortened=shortened.rsplit('\n\n',1)[0]
            result={'revision_id':revision['id'],'head_version':head['head_version'],'memory_epoch':head['memory_epoch'],
                'content_hash':revision['content_hash'],'selected_hash':hashlib.sha256(shortened.encode()).hexdigest(),
                'content_md':shortened,'truncated':len(shortened.encode())<len(encoded)}
            while len(json.dumps(result,ensure_ascii=False).encode())>min(1792,budget-200):
                if not result['content_md']:return None
                result['content_md']=result['content_md'].encode()[:-128].decode('utf-8',errors='ignore');result['truncated']=True
                result['selected_hash']=hashlib.sha256(result['content_md'].encode()).hexdigest()
            return result

    async def mutate(self,db,user_id,action,body,key,match,handler):
        if not key or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',key):
            raise BusinessError(40002,message='缺少有效幂等键',http_status=422)
        payload={'domain':'personal-memory','action':action,'body':body,'if_match':match}
        async with db.begin():
            # Lock the owner row first, including the no-head case. This also
            # serializes first-save/auto-init and duplicate-key requests.
            await self.authorize(db,user_id,lock=True)
            table=t.user_memory_requests
            row=(await db.execute(sa.select(table).where(table.c.user_id==user_id,table.c.action==action,
                table.c.request_key==key).with_for_update())).mappings().first()
            if row:
                if not hmac.compare_digest(row['payload_hash'],self.fingerprint(payload,row['hash_key_version'])):
                    error(40943,'MEMORY_IDEMPOTENCY_MISMATCH')
                if row['replay_until']<=utcnow():error(40929,'REQUEST_REPLAY_EXPIRED',410)
                if row['result_metadata'].get('result_job_id'):
                    from app.user_memory.jobs import MemoryJobs
                    return await MemoryJobs(self).get_job(db,user_id,row['result_metadata']['result_job_id'],replay=True)
                return await self.document(db,user_id,row['result_metadata'],purged=True)
            head=await self.head(db,user_id,lock=True,create=True)
            if not match:error(42801,'IF_MATCH_REQUIRED',428)
            if match!=etag(head['head_version']):error(40940,'MEMORY_VERSION_CONFLICT',head_version=head['head_version'])
            result=await handler(head)
            metadata={field:head[field] for field in ('user_id','active_revision_id','head_version','last_revision_number',
                'memory_epoch','enabled','auto_extract','management_mode','updated_at')}
            if isinstance(result,dict) and result.get('id'):metadata['result_job_id']=result['id']
            await db.execute(table.insert().values(id=uid(),user_id=user_id,action=action,request_key=key,
                payload_hash=self.fingerprint(payload),hash_key_version=self.keyring.active_version,status='succeeded',
                result_metadata=json_view(metadata),created_at=utcnow(),replay_until=utcnow()+timedelta(days=7),
                tombstone_until=utcnow()+timedelta(days=30)))
            return result if metadata.get('result_job_id') else await self.document(db,user_id,head)

    async def save(self,db,user_id,body,key,match):
        if isinstance(body.get('content_md'),str):normalize(body['content_md'])
        validate('MemorySaveInput',body);body={'content_md':normalize(body['content_md'])}
        async def operation(head):
            head['management_mode']='manual'
            await self.new_revision(db,head,body['content_md'],'user_edit')
        return await self.mutate(db,user_id,'save',body,key,match,operation)

    async def settings(self,db,user_id,body,key,match):
        validate('MemorySettingsInput',body)
        async def operation(head):
            head.update(body);head.update(head_version=head['head_version']+1,updated_at=utcnow())
            if body.get('enabled') is False or body.get('auto_extract') is False:
                head['memory_epoch']+=1
                await db.execute(t.user_memory_jobs.update().where(t.user_memory_jobs.c.user_id==user_id,
                    t.user_memory_jobs.c.status.in_(['queued','running','proposed'])).values(status='cancelled',updated_at=utcnow()))
            await self.update_head(db,head)
        return await self.mutate(db,user_id,'settings',body,key,match,operation)

    async def restore(self,db,user_id,revision_id,body,key,match):
        validate('MemoryEmptyInput',body)
        async def operation(head):
            revision=await self.revision(db,user_id,revision_id)
            head['management_mode']='manual'
            await self.new_revision(db,head,revision['content_md'],'restore')
        return await self.mutate(db,user_id,'restore',{'revision_id':revision_id},key,match,operation)

    async def clear(self,db,user_id,body,key,match):
        validate('MemoryClearInput',body)
        async def operation(head):
            head.update(enabled=False,auto_extract=False,memory_epoch=head['memory_epoch']+1,management_mode='manual')
            await db.execute(t.user_memory_heads.update().where(t.user_memory_heads.c.user_id==user_id).values(
                last_source_request_id=None,last_source_message_id=None,last_source_completed_at=None))
            await db.execute(t.user_memory_jobs.update().where(t.user_memory_jobs.c.user_id==user_id).values(
                status='cancelled',source_snapshot=None,proposed_content_md=None,proposed_content_hash=None,
                base_revision_id=None,applied_revision_id=None,
                error_reason='MEMORY_RESULT_PURGED' if body['purge_history'] else 'MEMORY_CLEARED',updated_at=utcnow()))
            if body['purge_history']:
                head['active_revision_id']=None;await self.update_head(db,head)
                rows=(await db.execute(sa.select(t.user_memory_revisions.c.id).where(
                    t.user_memory_revisions.c.user_id==user_id).order_by(t.user_memory_revisions.c.revision_number.desc()))).scalars().all()
                for revision_id in rows:await db.execute(t.user_memory_revisions.delete().where(
                    t.user_memory_revisions.c.user_id==user_id,t.user_memory_revisions.c.id==revision_id))
            await self.new_revision(db,head,'','clear')
        return await self.mutate(db,user_id,'clear',body,key,match,operation)

    async def revisions(self,db,user_id,limit=20,cursor=None):
        await self.authorize(db,user_id)
        table=t.user_memory_revisions
        where=[table.c.user_id==user_id]
        if cursor:where.append(table.c.revision_number<(await self.revision(db,user_id,cursor))['revision_number'])
        rows=(await db.execute(sa.select(table.c.id,table.c.revision_number,table.c.source_kind,table.c.content_hash,
            table.c.created_at).where(*where).order_by(table.c.revision_number.desc()).limit(limit+1))).mappings().all()
        return json_view({'items':rows[:limit],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None})
