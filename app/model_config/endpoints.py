"""Offline endpoint/profile checks. Saving a policy never performs network I/O."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from app.model_config.errors import fail


def canonical_url(value: str) -> tuple[str,str]:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(char in value for char in ('\\','\r','\n','\t','%'))):
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if not host or not re.fullmatch(r'[a-z0-9.:-]+',host):
            raise ValueError
        if ':' in host:
            ipaddress.IPv6Address(host)
            host = '['+host+']'
        port = parsed.port
        if port == 0:
            raise ValueError
        origin = parsed.scheme+'://'+host
        if port and port != (443 if parsed.scheme == 'https' else 80):
            origin += ':'+str(port)
        path = parsed.path or '/'
        if any(part in ('.','..') for part in path.split('/')) or '//' in path:
            raise ValueError
        return origin,path
    except (ValueError,UnicodeError):
        fail(40310,'ENDPOINT_DENIED',403)


def check_policy(value: dict) -> dict:
    origin,path = canonical_url(value['origin'])
    if path != '/':
        fail(40310,'ENDPOINT_DENIED',403)
    prefix = value['allowed_path_prefix']
    if canonical_url(origin+prefix)[1] != prefix:
        fail(40310,'ENDPOINT_DENIED',403)
    rules = value['connection_rules']
    # Closed fields; credential/header/proxy/certificate bypasses are not accepted.
    if (set(rules) - {'allowed_cidrs', 'allow_cross_service_fallback'}
            or not isinstance(rules.get('allowed_cidrs',[]),list)
            or type(rules.get('allow_cross_service_fallback', False)) is not bool):
        fail(40310,'ENDPOINT_DENIED',403)
    cidrs = rules.get('allowed_cidrs',[])
    if len(cidrs)>16 or any(not isinstance(item,str) for item in cidrs):
        fail(40310,'ENDPOINT_DENIED',403)
    try:
        networks = [ipaddress.ip_network(item,strict=True) for item in cidrs]
    except ValueError:
        fail(40310,'ENDPOINT_DENIED',403)
    if value['address_mode'] == 'public_https':
        if not origin.startswith('https://') or networks:
            fail(40310,'ENDPOINT_DENIED',403)
    else:
        # Explicit, narrowly scoped local/private networks for Ollama. Do not
        # permit metadata, link-local, unspecified, multicast or public ranges.
        permitted = [ipaddress.ip_network(item) for item in
                     ('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','::1/128','fc00::/7')]
        if not networks or any(not any(net.version==allowed.version and net.subnet_of(allowed)
            for allowed in permitted) for net in networks):
            fail(40310,'ENDPOINT_DENIED',403)
    host = urlsplit(origin).hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address:
        if (value['address_mode']=='public_https' and not address.is_global) or (
            value['address_mode']=='trusted_private' and not any(
                address.version==net.version and address in net for net in networks)):
            fail(40310,'ENDPOINT_DENIED',403)
    return {**value,'origin':origin,'connection_rules':{**rules,'allowed_cidrs':[str(net) for net in networks]}}


def check_profile(profile: dict, policy: dict):
    origin,path = canonical_url(profile['base_url'])
    prefix = policy['allowed_path_prefix'].rstrip('/')
    if (policy['status'] != 'active' or origin != policy['origin']
            or not (path == prefix or path.startswith(prefix+'/'))):
        fail(40310,'ENDPOINT_DENIED',403)
    combinations = {
        ('openai_compatible','chat'): {'chat_completions','responses'},
        ('openai_compatible','embedding'): {'openai_embeddings'},
        ('ollama_native','chat'): {'ollama_chat'},
        ('ollama_native','embedding'): {'ollama_embed'},
        ('dashscope_native','embedding'): {'dashscope_embed'},
    }
    if profile['api_protocol'] not in combinations.get((profile['adapter_type'],profile['model_kind']),set()):
        fail(40002,'ADAPTER_PROTOCOL_MISMATCH',422)
    allowed_auth = {'openai_compatible':{'none','bearer'},'ollama_native':{'none','bearer'},
                    'dashscope_native':{'dashscope_key'}}
    if profile['auth_mode'] not in allowed_auth[profile['adapter_type']]:
        fail(40002,'ADAPTER_AUTH_MISMATCH',422)
    # Explicitly whitelist scalar generation settings. No arbitrary SDK kwargs,
    # extra_body, nested credentials, URLs, HTTP clients or retry flags.
    limits = {'temperature':(0,2),'top_p':(0,1),'frequency_penalty':(-2,2),
              'presence_penalty':(-2,2),'seed':(-2147483648,2147483647)}
    if profile['api_protocol'] == 'responses':
        limits = {key: limits[key] for key in ('temperature', 'top_p')}
    for key,value in profile['parameters'].items():
        if (key not in limits or isinstance(value,bool) or not isinstance(value,(int,float))
                or not limits[key][0] <= value <= limits[key][1]
                or key=='seed' and not isinstance(value,int)):
            fail(40002,'UNSUPPORTED_MODEL_PARAMETER',422)
    counting = profile['token_counting_policy']
    if counting not in ({},{'mode':'conservative'},{'mode':'exact'}):
        fail(40002,'UNSUPPORTED_TOKEN_COUNTING_POLICY',422)
    if (profile['connect_timeout_ms']>90000 or profile['request_timeout_ms']>300000
            or profile['connect_timeout_ms']>profile['request_timeout_ms']):
        fail(40002,'INVALID_TIMEOUT_BUDGET',422)
    if profile['model_kind']=='chat' and profile['embedding_dimension'] is not None:
        fail(40002,'MODEL_KIND_MISMATCH',422)
    return origin
