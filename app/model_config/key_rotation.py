"""Staged key material and read-only, version-aware recovery checks. No activation."""
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import tempfile

from cryptography.fernet import Fernet
import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.crypto import Keyring
from app.model_config.errors import fail
from app.model_config.security import Principal,SCOPE,authorize,scoped
from app.model_config.service import utcnow


def prepare_rotation(source:Path,target:Path,new_version:str):
    if not isinstance(new_version,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',new_version):
        fail(40002,'KEY_VERSION_INVALID',422)
    original=Keyring.load(source)
    if new_version in original.keys:fail(40923,'KEY_VERSION_ALREADY_EXISTS')
    if len(original.keys)>=128:fail(40923,'KEYRING_VERSION_LIMIT')
    if source.resolve()==target.resolve() or target.exists() or target.is_symlink():
        fail(40923,'KEYRING_TARGET_EXISTS')
    keys={**original.keys,new_version:Fernet.generate_key()}
    prepared=Keyring(new_version,keys)
    payload={'active_version':prepared.active_version,'keys':{name:value.decode('ascii') for name,value in prepared.keys.items()}}
    target.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.keyring-rotation-',dir=target.parent)
    temporary=Path(temporary)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
            json.dump(payload,handle,sort_keys=True);handle.write('\n');handle.flush();os.fsync(handle.fileno())
        # Hard-link installation is atomic and cannot replace an existing name.
        # Unsupported filesystems fail closed; no rename/overwrite fallback.
        os.link(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
    return {'status':'prepared','previous_active_version':original.active_version,'active_version':new_version,
        'retained_versions':sorted(original.keys),'activation_required':True,'existing_credentials_rewritten':False}


async def _memory_table_present(db):
    dialect=db.get_bind().dialect.name
    if dialect=='mysql':
        query=sa.text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='user_memory_requests'")
    elif dialect=='sqlite':
        query=sa.text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='user_memory_requests'")
    else:fail(50303,'UNSUPPORTED_CONTROL_STORE',503)
    return bool((await db.execute(query)).scalar_one())


async def inspect_keyring(db,actor,keyring,*,check_decryption=False):
    """Point-in-time read, not a backup, live-file switch or permission to drop keys."""
    async with db.begin():
        await authorize(db,actor,'config.publish')
        now=utcnow();versions={};failures=[];failed=0;checked=0
        def entry(version):
            return versions.setdefault(version,{'key_version':version,'available':version in keyring.keys,
                'credential_counts':{},'config_replay_requests':0,'memory_replay_requests':0})
        for version in keyring.keys:entry(version)
        table=t.ai_secrets
        counts=(await db.execute(sa.select(table.c.key_version,table.c.status,sa.func.count().label('count'))
            .where(scoped(table)).group_by(table.c.key_version,table.c.status))).mappings().all()
        for row in counts:entry(row['key_version'])['credential_counts'][row['status']]=row['count']
        requests=t.ai_config_requests
        counts=(await db.execute(sa.select(requests.c.payload_hash_key_version,sa.func.count().label('count'))
            .where(scoped(requests),requests.c.replay_until>now).group_by(requests.c.payload_hash_key_version))).mappings().all()
        for row in counts:entry(row['payload_hash_key_version'])['config_replay_requests']=row['count']
        memory_present=await _memory_table_present(db)
        if memory_present:
            from app.user_memory.tables import user_memory_requests as memory
            counts=(await db.execute(sa.select(memory.c.hash_key_version,sa.func.count().label('count'))
                .where(memory.c.replay_until>now).group_by(memory.c.hash_key_version))).mappings().all()
            for row in counts:entry(row['hash_key_version'])['memory_replay_requests']=row['count']
        if check_decryption:
            cursor=None
            while True:
                query=sa.select(table).where(scoped(table))
                if cursor:query=query.where(table.c.id>cursor)
                rows=(await db.execute(query.order_by(table.c.id).limit(100))).mappings().all()
                for row in rows:
                    checked+=1
                    try:keyring.decrypt(row['ciphertext'],row['key_version'],{**SCOPE,'id':row['id'],
                        'origin':row['allowed_origin'],'version':row['credential_version']})
                    except BusinessError:
                        failed+=1
                        if len(failures)<100:failures.append({'credential_revision_id':row['id'],
                            'credential_version':row['credential_version'],'key_version':row['key_version'],'reason':'CREDENTIAL_DECRYPTION_FAILED'})
                if len(rows)<100:break
                cursor=rows[-1]['id']
        required=sorted(version for version,item in versions.items() if item['credential_counts'] or
            item['config_replay_requests'] or item['memory_replay_requests'])
        missing=[version for version in required if version not in keyring.keys]
        return {'active_version':keyring.active_version,'versions':[versions[key] for key in sorted(versions)],
            'required_versions':required,'missing_versions':missing,'memory_reference_table_present':memory_present,
            'decryption_check_requested':check_decryption,'checked_credentials':checked,'failed_credentials':failed,
            'credential_failures':failures,'credential_failures_truncated':failed>len(failures),'references_available':not missing,
            'credential_decryption_check_passed':failed==0 if check_decryption else None,
            'snapshot_kind':'database_read_transaction','key_removal_authorized':False,'secret_material_included':False}


async def inspect_from_cli(db,user_id,keyring,*,check_decryption=False):
    actor=Principal(user_id,None,utcnow()+timedelta(seconds=30))
    return await inspect_keyring(db,actor,keyring,check_decryption=check_decryption)
