"""Registry locking gate; only called with the harness-owned disposable DB."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy

import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_baseline_gate(engine,Database,actor,control,report):
    from app.core.failed_response import BusinessError
    from app.model_config import tables as t
    from app.model_config.embedding_status import EmbeddingStatusService
    service=EmbeddingStatusService(control)
    inventory={'inventory_version':1,'store_id':'a'*64,'store_snapshot':'b'*64,
        'tenant':'default_tenant','database':'default_database','collections':[
            {'index_key':'inventory_fixture','collection_name':'isolated_fixture','collection_id':'isolated-uuid',
             'dimension':3,'metadata_record_count':5,'status':'baseline_unverified','reason':'SEMANTIC_PROVENANCE_MISSING'}]}
    def call(payload,key):
        with Session(engine) as session:return asyncio.run(service.register(Database(session),actor.user_id,payload,key))
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(lambda _:call(inventory,'baseline-same-key'),[1,2]))
    assert values[0]==values[1] and values[0][1]==201
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(t.rag_index_versions)).scalar_one()==1
        request=connection.execute(sa.select(t.ai_config_requests).where(t.ai_config_requests.c.action=='index.baseline.register')).mappings().one()
        assert request['actor_access_jti'] is None and request['execution_state']['authority_kind']=='controlled_cli'
    report['checks']['config_mysql_concurrent_baseline_registration_one_request_one_version']=True
    changed=copy.deepcopy(inventory);changed['store_snapshot']='c'*64
    changed['collections'][0]['metadata_record_count']=6
    try:call(changed,'baseline-same-key')
    except BusinessError as error:assert error.code==40922
    else:raise AssertionError('Changed source must not overwrite an idempotent observation')
    new,_=call(changed,'baseline-new-observation')
    assert new['items'][0]['index_version']==2
    with engine.connect() as connection:
        indexes=connection.execute(sa.select(t.rag_index_versions).order_by(t.rag_index_versions.c.index_version)).mappings().all()
        assert len(indexes)==2 and all(row['semantic_fingerprint'] is None and row['status']=='baseline_unverified' for row in indexes)
        assert indexes[0]['semantic_identity']['observation']['metadata_record_count']==5
    report['checks']['config_mysql_changed_inventory_keeps_immutable_unverified_history']=True
    unknown=copy.deepcopy(inventory);unknown['collections'][0].update(dimension=None,reason='DIMENSION_UNKNOWN')
    try:call(unknown,'baseline-unknown-dimension')
    except BusinessError as error:assert error.data['reason']=='INDEX_BASELINE_NO_OBSERVED_DIMENSION'
    else:raise AssertionError('Unknown dimension must not create a placeholder version')
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(t.rag_index_versions)).scalar_one()==2
        assert connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_requests).where(
            t.ai_config_requests.c.request_key=='baseline-unknown-dimension')).scalar_one()==0
    report['checks']['config_mysql_unknown_dimension_rolls_back_without_placeholder']=True
    with engine.connect() as connection:
        assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==2
        assert connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_releases)).scalar_one()==2
        assert connection.execute(sa.select(sa.func.count()).select_from(t.outbox_events)).scalar_one()==2
    report['checks']['config_mysql_baseline_registration_never_moves_active_release']=True
