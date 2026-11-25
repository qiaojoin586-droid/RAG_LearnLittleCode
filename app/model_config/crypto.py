"""Versioned, purpose-separated keyring. Missing keys never trigger regeneration."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken

from app.model_config.contracts import canonical
from app.model_config.errors import fail


def default_keyring_path() -> Path:
    return Path(os.getenv('AI_CONFIG_KEYRING_FILE','data/config-secrets/keyring.json'))


@dataclass(frozen=True, repr=False)
class Keyring:
    active_version: str
    keys: Mapping[str, bytes]

    def __post_init__(self):
        if not isinstance(self.active_version,str) or self.active_version not in self.keys:
            raise ValueError('Active key version is missing')
        for version,value in self.keys.items():
            if not isinstance(version,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',version):
                raise ValueError('Invalid key version')
            Fernet(value)
        object.__setattr__(self,'keys',MappingProxyType(dict(self.keys)))

    def __repr__(self):
        return '<Keyring redacted>'

    @classmethod
    def load(cls, path: Path | None = None):
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError('Duplicate keyring field')
                value[key]=item
            return value
        loaded=None
        try:
            source=path or default_keyring_path()
            if source.stat().st_size>1048576:raise ValueError('Keyring too large')
            value = json.loads(source.read_text(encoding='utf-8-sig'),object_pairs_hook=unique)
            if not isinstance(value,dict) or set(value)!={'active_version','keys'}:raise ValueError('Invalid keyring fields')
            if not isinstance(value['keys'],dict) or not 1<=len(value['keys'])<=128:raise ValueError('Invalid keyring versions')
            loaded=cls(value['active_version'],{key:encoded.encode('ascii') for key,encoded in value['keys'].items()})
        except (OSError,ValueError,KeyError,TypeError,UnicodeError,AttributeError):
            pass
        if loaded is None:fail(50303,'KEYRING_UNAVAILABLE',503)
        return loaded

    @classmethod
    def create(cls, path: Path | None = None):
        target = path or default_keyring_path()
        target.parent.mkdir(parents=True,exist_ok=True)
        value = {'active_version':'local-v1','keys':{'local-v1':Fernet.generate_key().decode('ascii')}}
        descriptor = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
            json.dump(value,handle)
            handle.flush()
            os.fsync(handle.fileno())
        return cls.load(target)

    def purpose_key(self, purpose: str, version: str | None = None) -> bytes:
        try:
            master = base64.urlsafe_b64decode(self.keys[version or self.active_version])
        except KeyError:
            fail(50303,'KEY_VERSION_UNAVAILABLE',503)
        return hmac.new(master,('raglearn/model-config/'+purpose).encode(),hashlib.sha256).digest()

    def fingerprint(self, payload, version: str | None = None) -> str:
        return hmac.new(self.purpose_key('idempotency',version),canonical(payload),hashlib.sha256).hexdigest()

    def encrypt(self, value: str, identity: dict) -> tuple[bytes,str]:
        encrypted = Fernet(self.keys[self.active_version]).encrypt(canonical({'identity':identity,'value':value}))
        return encrypted,self.active_version

    def decrypt(self, encrypted: bytes, version: str, identity: dict) -> str:
        try:
            decoded = json.loads(Fernet(self.keys[version]).decrypt(encrypted))
            if decoded['identity'] != identity or not isinstance(decoded['value'],str):
                raise InvalidToken
            return decoded['value']
        except (InvalidToken,KeyError,TypeError,ValueError,UnicodeError):
            fail(50303,'CREDENTIAL_DECRYPTION_FAILED',503)
