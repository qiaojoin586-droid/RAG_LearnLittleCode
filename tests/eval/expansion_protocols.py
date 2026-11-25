"""Exercise M3-15 capability probes against an isolated loopback Ollama process."""
import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ablation_environment import local_ollama
from app.model_config.probes import run_suite
from app.model_config.provider import ProviderGateway


async def measure(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent, prefix='ollama-') as tmp:
        async with local_ollama(args.ollama, args.models, Path(tmp)) as (http, models):
            origin = str(http.base_url).rstrip('/')
            version = (await http.get('/api/version')).json()['version']
            evidence = {'model':args.model, 'digest':models[args.model], 'ollama_version':version, 'probes':{}}
            for protocol in ('ollama_chat','chat_completions','responses'):
                native = protocol == 'ollama_chat'
                policy = {'policy_key':'local-evaluation', 'origin':origin, 'allowed_path_prefix':'/' if native else '/v1',
                          'address_mode':'trusted_private', 'connection_rules':{'allowed_cidrs':['127.0.0.1/32']},
                          'environment':'development'}
                profile = {'api_protocol':protocol, 'adapter_type':'ollama_native' if native else 'openai_compatible',
                           'model':args.model, 'auth_mode':'none', 'connect_timeout_ms':5000, 'request_timeout_ms':90000,
                           'max_output_tokens':384, 'base_url':origin if native else origin+'/v1',
                           'parameters':{'temperature':0}, 'vendor':'ollama'}

                async def permit():
                    if http.base_url.host != '127.0.0.1':
                        raise ValueError('Evaluation requires loopback')

                async with ProviderGateway(profile,policy,None,permit=permit) as gateway:
                    evidence['probes'][protocol] = await run_suite(gateway,'rag_query_expand')
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print({key:probe['status'] for key,probe in evidence['probes'].items()})
    return all(probe['status']=='passed' and probe['call_count']==1 for probe in evidence['probes'].values())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ollama',default=shutil.which('ollama'))
    parser.add_argument('--models',type=Path,default=Path.home()/'.ollama/models')
    parser.add_argument('--model',default='qwen3:0.6b')
    parser.add_argument('--output',type=Path,default=ROOT/'.cache/upgrade-m3-expansion-role/ollama-protocols.json')
    raise SystemExit(0 if asyncio.run(measure(parser.parse_args())) else 1)
