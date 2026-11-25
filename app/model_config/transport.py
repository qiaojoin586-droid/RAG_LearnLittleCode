"""Policy-bound HTTP transport: validate DNS, dial a literal IP, retain TLS SNI.

The network backend is supplied through httpcore's documented backend interface.
There is no proxy/environment inheritance, redirect following or connection retry.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

import httpcore
from httpcore._backends.anyio import AnyIOBackend
import httpx

from app.model_config.endpoints import canonical_url, check_policy
from app.model_config.errors import fail

MAX_RESPONSE_BYTES = 2*1024*1024


def allowed_address(value: str, policy: dict) -> bool:
    try:
        address = ipaddress.ip_address(value)
        if getattr(address,'ipv4_mapped',None):
            address = address.ipv4_mapped
        if address in (ipaddress.ip_address('168.63.129.16'),ipaddress.ip_address('100.100.100.200')):
            return False
        if address.is_link_local or address.is_unspecified or address.is_multicast or (address.is_reserved and not address.is_loopback):
            return False
        if policy['address_mode']=='public_https':
            return address.is_global
        return any(address.version==network.version and address in network for network in (
            ipaddress.ip_network(item) for item in policy['connection_rules']['allowed_cidrs']))
    except ValueError:
        return False


async def resolve_addresses(host: str, port: int) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        entries = await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
        return sorted({entry[4][0] for entry in entries})


class PolicyNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy, *, resolver=resolve_addresses, backend=None):
        self.policy = policy
        self.resolver = resolver
        self.backend = backend or AnyIOBackend()

    async def connect_tcp(self,host,port,timeout=None,local_address=None,socket_options=None):
        endpoint = urlsplit(self.policy['origin'])
        if host != endpoint.hostname or port != (endpoint.port or (443 if endpoint.scheme=='https' else 80)):
            fail(40310,'ENDPOINT_DENIED',403)
        budget = min(timeout or 5,5)
        try:
            addresses = await asyncio.wait_for(self.resolver(host,port),budget)
        except TimeoutError:
            raise httpcore.ConnectTimeout('Provider DNS lookup timed out') from None
        if not addresses or not all(allowed_address(address,self.policy) for address in addresses):
            fail(40310,'ENDPOINT_DNS_DENIED',403)
        # One vetted address, no second hostname resolution or implicit retries.
        stream = await self.backend.connect_tcp(addresses[0],port,timeout=timeout,
            local_address=local_address,socket_options=socket_options)
        peer = stream.get_extra_info('server_addr')
        if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(addresses[0]):
            await stream.aclose()
            fail(40310,'ENDPOINT_PEER_MISMATCH',403)
        # httpcore calls start_tls(server_hostname=original hostname) on this
        # stream, so certificate verification does not use the numeric address.
        return stream

    async def connect_unix_socket(self,*args,**kwargs):
        fail(40310,'ENDPOINT_DENIED',403)

    async def sleep(self,seconds):
        await asyncio.sleep(seconds)


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self,stream):
        self.stream = stream
    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size>MAX_RESPONSE_BYTES:
                fail(42222,'PROVIDER_RESPONSE_TOO_LARGE',422)
            yield chunk
    async def aclose(self):
        await self.stream.aclose()


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy: dict, *, permit, resolver=resolve_addresses, backend=None):
        normalized = check_policy({key:policy[key] for key in (
            'policy_key','origin','allowed_path_prefix','address_mode','connection_rules','environment')})
        self.policy = normalized
        self.permit = permit
        self.pool = httpcore.AsyncConnectionPool(ssl_context=ssl.create_default_context(),
            max_connections=2,max_keepalive_connections=0,retries=0,http2=False,
            network_backend=PolicyNetworkBackend(normalized,resolver=resolver,backend=backend))

    async def handle_async_request(self, request):
        origin,path = canonical_url(str(request.url))
        prefix = self.policy['allowed_path_prefix'].rstrip('/')
        if (origin != self.policy['origin'] or not (path==prefix or path.startswith(prefix+'/'))
                or request.method not in ('GET','POST')):
            fail(40310,'ENDPOINT_DENIED',403)
        await self.permit()
        response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,url=httpcore.URL(scheme=request.url.raw_scheme,
                host=request.url.raw_host,port=request.url.port,target=request.url.raw_path),
            headers=request.headers.raw,content=request.stream,extensions=request.extensions))
        return httpx.Response(response.status,headers=response.headers,
                              stream=LimitedStream(response.stream),extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()
