"""Live configuration authorization and access-token-bound reauthentication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac

from jose import jwt, JWTError
import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.crypto import Keyring
from app.model_config.errors import fail
from app.model_config.permissions import allows_config_action
from app.models.user import User

SCOPE = {'scope_type':'system','scope_id':'system'}


def scoped(table):
    return sa.and_(table.c.scope_type == 'system', table.c.scope_id == 'system')


@dataclass(frozen=True)
class Principal:
    user_id: str
    access_jti: str
    expires_at: datetime


async def authorize(db, principal: Principal, permission: str, *, lock=False):
    # Global write lock: authorization head -> user/grant -> config head ->
    # draft -> version counters/references. All configuration writers use it.
    query = sa.select(t.ai_config_authorization_heads).where(scoped(t.ai_config_authorization_heads))
    head = (await db.execute(query.with_for_update() if lock else query)).mappings().first()
    user_query = sa.select(User.__table__).where(User.uuid == principal.user_id)
    user = (await db.execute(user_query.with_for_update() if lock else user_query)).mappings().first()
    if not user or user['status'] != 'active':
        fail(40314,'ACCOUNT_INACTIVE',403)
    grant_query = sa.select(t.ai_config_admin_grants).where(scoped(t.ai_config_admin_grants),
        t.ai_config_admin_grants.c.subject_user_id == principal.user_id)
    grant = (await db.execute(grant_query.with_for_update() if lock else grant_query)).mappings().first()
    if (not head or not grant or grant['status'] != 'active'
            or not allows_config_action(grant['config_role_key'],permission)):
        fail(40312,'CONFIG_PERMISSION_DENIED',403)
    if principal.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        fail(40102,'TOKEN_INVALID',401)
    return user,grant,head['policy_version']


def issue_proof(keyring: Keyring, principal: Principal, policy_version: int) -> dict:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=600)
    payload = {'sub':principal.user_id,'access_jti':principal.access_jti,**SCOPE,
        'iat':int(now.timestamp()),'exp':int(expires.timestamp()),'proof_type':'config_reauth',
        'policy_version':policy_version,'iss':'raglearn-config','aud':'model-config'}
    proof = jwt.encode(payload,keyring.purpose_key('reauth'),algorithm='HS256',
                       headers={'kid':keyring.active_version})
    return {'proof':proof,'proof_type':'config_reauth','expires_in':600,
            'expires_at':expires.isoformat(),'actor_id':principal.user_id,**SCOPE}


def verify_proof(keyring: Keyring, proof: str | None, principal: Principal, policy_version: int):
    if not proof:
        fail(40110,'REAUTH_REQUIRED',401)
    try:
        version = jwt.get_unverified_header(proof)['kid']
        # A retired signing version cannot continue authorizing sensitive writes.
        if not isinstance(version,str) or version != keyring.active_version:
            raise ValueError
        value = jwt.decode(proof,keyring.purpose_key('reauth',version),algorithms=['HS256'],
            issuer='raglearn-config',audience='model-config',
            options={'require_exp':True,'require_iat':True,'require_sub':True})
        expected = {'sub':principal.user_id,'access_jti':principal.access_jti,
                    'policy_version':policy_version,'proof_type':'config_reauth',**SCOPE}
        if any(value.get(key) != item for key,item in expected.items()):
            raise ValueError
        now = int(datetime.now(timezone.utc).timestamp())
        if not (now-600 <= value['iat'] <= now and value['exp']-value['iat'] == 600):
            raise ValueError
        # Constant time comparison is also used for the token binding.
        if not hmac.compare_digest(value['access_jti'],principal.access_jti):
            raise ValueError
        return datetime.fromtimestamp(value['exp'],timezone.utc).replace(tzinfo=None)
    except (JWTError,KeyError,ValueError,TypeError):
        fail(40111,'REAUTH_INVALID',401)
