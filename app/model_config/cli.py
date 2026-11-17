"""Explicit local setup: python -m app.model_config.cli --help."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='本地模型配置初始化与旧配置迁移；不自动发布或调用供应商')
    commands = parser.add_subparsers(dest='command',required=True)
    key = commands.add_parser('init-keyring',help='显式创建主密钥文件；已有文件绝不覆盖')
    key.add_argument('--path',type=Path)
    rotation=commands.add_parser('prepare-keyring-rotation',help='生成独立的新版本密钥文件；保留旧版本，不覆盖、不切换业务配置')
    rotation.add_argument('--source',type=Path)
    rotation.add_argument('--output',type=Path,required=True)
    rotation.add_argument('--new-version',required=True)
    inspection=commands.add_parser('inspect-keyring',help='管理员只读盘点密文/HMAC 版本引用，可选验证解密；不移除旧密钥')
    inspection.add_argument('--path',type=Path)
    inspection.add_argument('--user-id',required=True)
    inspection.add_argument('--check-decryption',action='store_true')
    pricing = commands.add_parser('register-price', help='登记一版从当前时间开始生效的模型单价')
    for field in ('user-id', 'origin', 'model', 'currency', 'input-price', 'output-price', 'source', 'request-key'):
        pricing.add_argument(f'--{field}', required=True)
    pricing.add_argument('--unit-tokens', required=True, type=int)
    admin = commands.add_parser('bootstrap-admin',help='将现有 active 用户设为首位配置管理员；只允许首次运行')
    admin.add_argument('--user-id',required=True)
    attest=commands.add_parser('attest-indexes',help='管理员核验原始建库记录并绑定草稿；不发布、不推测旧模型来源')
    attest.add_argument('--user-id',required=True)
    attest.add_argument('--draft-id',required=True)
    attest.add_argument('--if-match',required=True)
    attest.add_argument('--record',required=True,type=Path)
    attest.add_argument('--store',required=True,type=Path)
    attest.add_argument('--index',required=True,action='append',metavar='INDEX_KEY=BASELINE_UUID')
    attest.add_argument('--tenant',default='default_tenant')
    attest.add_argument('--database',default='default_database')
    attest.add_argument('--request-key',required=True)
    attest.add_argument('--migration', action='store_true', help='绑定独立构建的候选集合，发布时复核追平状态')
    for name in ('inspect-indexes','register-index-baselines'):
        command=commands.add_parser(name,help='只读检查现有 Chroma 集合目录' if name=='inspect-indexes' else '登记待核验索引基线，不绑定、不重建或发布')
        command.add_argument('--store',type=Path,help='默认读取 config/chroma.yaml 的存储目录')
        command.add_argument('--collection',action='append',default=[],metavar='INDEX_KEY=COLLECTION_NAME')
        command.add_argument('--tenant',default='default_tenant')
        command.add_argument('--database',default='default_database')
        if name=='register-index-baselines':
            command.add_argument('--user-id',required=True,help='已有 active 配置管理员 UUID')
            command.add_argument('--request-key',default='index-baseline:system:system:v1')
    for name in ('preview-legacy','import-legacy'):
        command=commands.add_parser(name,help='只读预览旧配置' if name=='preview-legacy' else '受控 CLI 导入为待审核草稿，不发布')
        command.add_argument('--agent-config',type=Path,default=Path('config/agent.yaml'))
        command.add_argument('--ppt-config',type=Path,default=Path('config/ppt.yaml'))
        if name=='import-legacy':
            command.add_argument('--user-id',required=True,help='已有且 active 的配置管理员 UUID')
            command.add_argument('--endpoint-policy',action='append',default=[],metavar='ORIGIN=REVISION_UUID',help='明确使用已授权的端点策略；可重复')
            command.add_argument('--request-key',default='legacy-import:system:system:v1',help='同一来源重试沿用此键；更换来源须显式换键新建草稿')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    if args.command=='init-keyring':
        from app.model_config.crypto import Keyring,default_keyring_path
        target = args.path or default_keyring_path()
        Keyring.create(target)
        print(json.dumps({'status':'created','path':str(target.resolve())},ensure_ascii=False))
        return
    async def execute(plan=None):
        from app.db.database import async_session_factory,close_db
        from app.model_config.service import bootstrap_admin
        try:
            async with async_session_factory() as db:
                if args.command == 'register-price':
                    from app.model_config.pricing import register_price
                    from app.model_config.service import ConfigService
                    result, _ = await register_price(ConfigService(), db, args.user_id, origin=args.origin, model=args.model,
                        currency=args.currency, unit_tokens=args.unit_tokens, input_price=args.input_price,
                        output_price=args.output_price, source=args.source, request_key=args.request_key)
                    return result
                if args.command=='inspect-keyring':
                    from app.model_config.key_rotation import inspect_from_cli
                    from app.model_config.crypto import Keyring
                    return await inspect_from_cli(db,args.user_id,Keyring.load(args.path),check_decryption=args.check_decryption)
                if args.command=='attest-indexes':
                    from app.model_config.service import ConfigService
                    from app.model_config.index_attestation import IndexAttestationService
                    result,_=await IndexAttestationService(ConfigService()).attach(db,args.user_id,args.draft_id,plan,
                        args.request_key,args.if_match,migration=args.migration)
                    return result
                if args.command=='register-index-baselines':
                    from app.model_config.service import ConfigService
                    from app.model_config.embedding_status import EmbeddingStatusService
                    result,_=await EmbeddingStatusService(ConfigService()).register(db,args.user_id,plan,args.request_key)
                    return result
                if args.command=='import-legacy':
                    from app.model_config.service import ConfigService
                    from app.model_config.legacy_import import LegacyImporter
                    policies={}
                    for entry in args.endpoint_policy:
                        origin,separator,revision=entry.partition('=')
                        if not separator or not revision or origin in policies:
                            raise ValueError('Invalid endpoint mapping')
                        from app.model_config.endpoints import canonical_url
                        normalized,path=canonical_url(origin)
                        if normalized!=origin or path!='/':raise ValueError('Origin required')
                        policies[origin]=revision
                    result,_=await LegacyImporter(ConfigService()).apply(db,args.user_id,plan,policies,args.request_key)
                    return result
                return await bootstrap_admin(db,args.user_id)
        finally:
            await close_db()
    from app.core.failed_response import BusinessError
    try:
        plan=None
        if args.command=='prepare-keyring-rotation':
            from app.model_config.key_rotation import prepare_rotation
            from app.model_config.crypto import default_keyring_path
            print(json.dumps(prepare_rotation(args.source or default_keyring_path(),args.output,args.new_version),ensure_ascii=False))
            return
        if args.command=='attest-indexes':
            from app.model_config.index_attestation import prepare_attestation
            indexes={}
            for entry in args.index:
                key,separator,value=entry.partition('=')
                if not separator or key in indexes:raise ValueError('Invalid index mapping')
                indexes[key]=value
            plan=prepare_attestation(args.record,args.store,indexes,tenant=args.tenant,database=args.database)
        if args.command in ('inspect-indexes','register-index-baselines'):
            from app.model_config.index_inventory import inspect_indexes
            from app.utils.config import get_chroma_config,BASE_DIR
            config=get_chroma_config()
            directory=args.store or (BASE_DIR/config.get('persist_directory','data/chroma'))
            collections={}
            for entry in args.collection:
                key,separator,value=entry.partition('=')
                if not separator or not value or key in collections:raise ValueError('Invalid collection mapping')
                collections[key]=value
            plan=inspect_indexes(directory,collections or config.get('collections'),tenant=args.tenant,database=args.database)
            if args.command=='inspect-indexes':
                print(json.dumps(plan,ensure_ascii=False));return
        if args.command in ('preview-legacy','import-legacy'):
            import yaml
            from app.model_config.legacy_import import build_plan
            def load(path):
                if path.stat().st_size>1048576:raise ValueError('Config too large')
                value=yaml.safe_load(path.read_text(encoding='utf-8')) or {}
                if not isinstance(value,dict):raise ValueError('Object required')
                return value
            plan=build_plan(os.environ,load(args.agent_config),ppt_timeout=load(args.ppt_config).get('ppt',{}).get('outline_timeout',45))
            if args.command=='preview-legacy':
                print(json.dumps(plan.preview(),ensure_ascii=False));return
        result=asyncio.run(execute(plan))
        print(json.dumps(result,ensure_ascii=False))
        if args.command=='inspect-keyring' and (not result['references_available'] or result['failed_credentials']):
            raise SystemExit(2)
    except BusinessError as error:
        parser.exit(1,error.message+'\n')
    except Exception as error:
        # DB/SQL/YAML exception messages can embed source values. Show only the
        # exception class here, never SQL parameters, config text or a traceback.
        parser.exit(1,'本地配置操作未完成：'+type(error).__name__+'\n')


if __name__=='__main__':
    main()
