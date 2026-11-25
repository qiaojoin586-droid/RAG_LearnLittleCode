"""Real local Chroma/SQLite observations, scoped SQL registry and space checks."""
import builtins
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.embedding_space import compatibility,semantic_fingerprint
from app.model_config.embedding_status import EmbeddingStatusService
from app.model_config.index_inventory import inspect_indexes
from app.model_config.service import utcnow
from test_model_config_control_plane import admin,store,api,AsyncFacade  # noqa: F401
from test_model_config_publication import publish


@pytest.fixture
def catalog(tmp_path):
    directory=tmp_path/'chroma';directory.mkdir()
    connection=sqlite3.connect(directory/'chroma.sqlite3')
    connection.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE databases(id TEXT PRIMARY KEY,name TEXT,tenant_id TEXT);
        CREATE TABLE collections(id TEXT PRIMARY KEY,name TEXT,dimension INTEGER,database_id TEXT);
        CREATE TABLE segments(id TEXT PRIMARY KEY,collection TEXT,scope TEXT);
        CREATE TABLE embeddings(id INTEGER PRIMARY KEY,segment_id TEXT,embedding_id TEXT);
        INSERT INTO databases VALUES('db','default_database','default_tenant'),('other','default_database','other_tenant');
        INSERT INTO collections VALUES('rag','rag_collection',3,'db'),('notes','notes_collection',NULL,'db'),('foreign','rag_collection',9,'other');
        INSERT INTO segments VALUES('rag-metadata','rag','METADATA'),('rag-vector','rag','VECTOR'),('other-metadata','foreign','METADATA');
        INSERT INTO embeddings VALUES(1,'rag-metadata','private-document-id'),(2,'rag-metadata','private-note-id'),(3,'rag-vector','not-metadata');
    ''')
    yield directory,connection
    connection.close()


def snapshot_files(directory):
    return {p.name:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in directory.iterdir() if p.is_file()}


def test_catalog_observation_reads_wal_without_mutating_source_or_inventing_provenance(catalog,tmp_path):
    directory,_=catalog;before=snapshot_files(directory)
    inventory=inspect_indexes(directory,workspace=tmp_path)
    assert snapshot_files(directory)==before
    rag,notes=inventory['collections'][1],inventory['collections'][0]
    assert rag['index_key']=='rag' and rag['dimension']==3 and rag['metadata_record_count']==2
    assert rag['status']=='baseline_unverified' and rag['reason']=='SEMANTIC_PROVENANCE_MISSING'
    assert notes['dimension'] is None and notes['reason']=='DIMENSION_UNKNOWN'
    assert 'private-document-id' not in json.dumps(inventory) and str(directory) not in json.dumps(inventory)
    assert inspect_indexes(directory,workspace=tmp_path)==inventory
    assert not list((tmp_path/'.cache/index-inventory').iterdir())


def test_real_installed_chroma_catalog_and_metadata_counts(tmp_path,monkeypatch):
    for name in ('TEMP','TMP','SQLITE_TMPDIR'):monkeypatch.setenv(name,str(tmp_path))
    import chromadb
    from chromadb.config import Settings
    directory=tmp_path/'real-chroma'
    directory.mkdir()
    # Keep all SDK database and scratch files in the isolated workspace fixture.
    relative=os.path.relpath(directory,Path.cwd()).replace('\\','/')
    with chromadb.PersistentClient(path=relative,settings=Settings(anonymized_telemetry=False)) as client:
        rag=client.get_or_create_collection('rag_collection',embedding_function=None)
        client.get_or_create_collection('notes_collection',embedding_function=None)
        rag.upsert(ids=['a','b'],embeddings=[[1.,0.,0.],[0.,1.,0.]],documents=['private alpha','private beta'])
    before=snapshot_files(directory)
    inventory=inspect_indexes(directory,workspace=tmp_path)
    item=next(row for row in inventory['collections'] if row['index_key']=='rag')
    assert item['dimension']==3 and item['metadata_record_count']==2
    assert 'private alpha' not in json.dumps(inventory)
    assert snapshot_files(directory)==before


def test_tenant_database_and_missing_collection_are_explicit(catalog,tmp_path):
    directory,_=catalog
    other=inspect_indexes(directory,{'rag':'rag_collection'},tenant='other_tenant',workspace=tmp_path)
    assert other['collections'][0]['dimension']==9 and other['collections'][0]['metadata_record_count']==0
    missing=inspect_indexes(directory,{'custom':'missing_collection'},workspace=tmp_path)
    assert missing['collections'][0]['reason']=='COLLECTION_MISSING'
    assert missing['collections'][0]['metadata_record_count'] is None
    assert other['store_id']!=missing['store_id']


def test_missing_and_unsupported_catalog_do_not_create_or_initialize_a_store(tmp_path):
    missing=tmp_path/'missing'
    with pytest.raises(BusinessError) as error:inspect_indexes(missing,workspace=tmp_path)
    assert error.value.data['reason']=='INDEX_STORE_NOT_FOUND' and not missing.exists()
    source=tmp_path/'unsupported';source.mkdir()
    with sqlite3.connect(source/'chroma.sqlite3') as connection:connection.execute('CREATE TABLE collections(name TEXT)')
    with pytest.raises(BusinessError) as error:inspect_indexes(source,workspace=tmp_path)
    assert error.value.data['reason']=='INDEX_CATALOG_LAYOUT_UNSUPPORTED'


def test_catalog_copy_detects_concurrent_source_changes(catalog,tmp_path,monkeypatch):
    from app.model_config import index_inventory
    directory,connection=catalog;original=index_inventory.shutil.copyfile
    def changed(source,destination):
        value=original(source,destination)
        if Path(source).name=='chroma.sqlite3':
            connection.execute("UPDATE collections SET dimension=4 WHERE id='rag'");connection.commit()
        return value
    monkeypatch.setattr(index_inventory.shutil,'copyfile',changed)
    with pytest.raises(BusinessError) as error:inspect_indexes(directory,workspace=tmp_path)
    assert error.value.data['reason']=='INDEX_STORE_CHANGED'
    assert not list((tmp_path/'.cache/index-inventory').iterdir())


def identity():
    return {'schema_version':1,'vendor':'ollama','model':'model-a','weights_revision':'sha256:immutable-a',
        'dimension':3,'normalization':'l2','input_preprocessing':'utf8-verbatim-v1',
        'document_prefix':'doc:','query_prefix':'query:','pooling':'mean','parameters':{}}


def space_pair():
    value=identity();digest=semantic_fingerprint(value)
    return ({'dimension':3,'status':'baseline_verified','semantic_identity':value,'semantic_fingerprint':digest},
        {'model_kind':'embedding','model':'model-a','vendor':'ollama','model_revision':value['weights_revision'],
         'embedding_dimension':3,'semantic_identity':copy.deepcopy(value),'semantic_fingerprint':digest})


def test_connection_key_url_timeout_changes_do_not_change_semantic_space():
    index,profile=space_pair();profile.update(base_url='https://second.example/v1',credential_revision_id='new-key',request_timeout_ms=90000)
    assert compatibility(index,profile)=='COMPATIBLE'
    assert semantic_fingerprint(dict(reversed(list(identity().items()))))==index['semantic_fingerprint']


@pytest.mark.parametrize('field,value',[
    ('weights_revision','sha256:immutable-b'),('model','same-dimension-other-model'),('normalization','none'),
    ('input_preprocessing','strip-v1'),('document_prefix','new:'),('query_prefix','new:'),('pooling','last-token'),
])
def test_equal_dimensions_never_hide_changes_to_semantic_space(field,value):
    index,profile=space_pair();profile['semantic_identity'][field]=value
    profile['semantic_fingerprint']=semantic_fingerprint(profile['semantic_identity'])
    profile['model']=profile['semantic_identity']['model'];profile['model_revision']=profile['semantic_identity']['weights_revision']
    assert compatibility(index,profile)=='EMBEDDING_SPACE_MISMATCH'


@pytest.mark.parametrize('change,reason',[
    (lambda i,p:i.update(status='retired'),'INDEX_BASELINE_UNVERIFIED'),
    (lambda i,p:i.update(status='baseline_unverified'),'INDEX_BASELINE_UNVERIFIED'),
    (lambda i,p:p.update(semantic_fingerprint=None),'QUERY_SPACE_UNVERIFIED'),
    (lambda i,p:p.update(embedding_dimension=4),'EMBEDDING_DIMENSION_MISMATCH'),
    (lambda i,p:p.update(semantic_fingerprint='0'*64),'SEMANTIC_FINGERPRINT_INVALID'),
    (lambda i,p:p.update(model='changed-without-evidence'),'QUERY_IDENTITY_PROFILE_MISMATCH'),
    (lambda i,p:p.update(semantic_identity={}),'SEMANTIC_IDENTITY_UNVERIFIED'),
])
def test_unverified_retired_or_corrupt_evidence_cannot_be_compatible(change,reason):
    index,profile=space_pair();change(index,profile)
    assert compatibility(index,profile)==reason


@pytest.mark.parametrize('extra',[{'api_key':'secret'},{'weights_revision':'latest'},{'dimension':True},{'schema_version':True},
    {'parameters':{'api_key':'secret'}},{'parameters':{'dimensions':True}}])
def test_semantic_identity_rejects_unknown_revision_and_connection_secrets(extra):
    value=identity();value.update(extra)
    with pytest.raises(BusinessError) as error:semantic_fingerprint(value)
    assert 'secret' not in str(error.value.data)


async def register(admin,inventory,key='baseline',actor=None):
    with Session(admin.engine) as session:
        return await EmbeddingStatusService(admin.service).register(AsyncFacade(session),actor or admin.actor.user_id,inventory,key)


async def status(admin,**kwargs):
    with Session(admin.engine) as session:
        return await EmbeddingStatusService(admin.service).status(AsyncFacade(session),admin.actor,**kwargs)


@pytest.mark.asyncio
async def test_registration_is_scoped_idempotent_audited_and_never_activates_vectors(admin,catalog,tmp_path):
    inventory=inspect_indexes(catalog[0],workspace=tmp_path)
    result,http=await register(admin,inventory)
    assert http==201 and result['status']=='baseline_unverified' and not result['published']
    assert result['skipped']==[{'index_key':'notes','reason':'DIMENSION_UNKNOWN'}]
    assert (await register(admin,inventory))[0]==result
    assert (await register(admin,inventory,'another-request'))[0]==result
    assert admin.count(t.rag_index_versions)==1
    row=admin.rows(t.rag_index_versions)[0]
    assert row['semantic_fingerprint'] is None and row['build_profile_revision_id'] is None
    assert row['parser_version']==row['chunker_version']=='unknown'
    assert admin.count(t.ai_config_releases)==admin.count(t.outbox_events)==admin.count(t.ai_config_drafts)==0
    request=admin.rows(t.ai_config_requests)[0]
    assert request['result_ref']==result['items'][0]['id']
    assert request['actor_access_jti'] is None and request['execution_state']['authority_kind']=='controlled_cli'
    view=await status(admin)
    assert view['items'][0]['dimension']==3 and view['items'][0]['metadata_record_count']==2
    assert not view['items'][0]['semantic_provenance_verified'] and not view['items'][0]['runtime_usable']
    assert view['migration_supported'] and view['runtime_binding_supported']
    assert str(catalog[0]) not in json.dumps(view) and 'semantic_identity' not in json.dumps(view)


@pytest.mark.asyncio
async def test_changed_inventory_requires_new_key_and_keeps_old_baseline(admin,catalog,tmp_path):
    inventory=inspect_indexes(catalog[0],workspace=tmp_path);await register(admin,inventory)
    connection=catalog[1];connection.execute("INSERT INTO embeddings VALUES(4,'rag-metadata','new')");connection.commit()
    changed=inspect_indexes(catalog[0],workspace=tmp_path)
    with pytest.raises(BusinessError) as error:await register(admin,changed)
    assert error.value.code==40922
    await register(admin,changed,'new-observation')
    assert admin.count(t.rag_index_versions)==2
    current=await status(admin)
    assert len(current['items'])==1 and current['items'][0]['index_version']==2
    assert current['items'][0]['metadata_record_count']==3


@pytest.mark.asyncio
async def test_no_unknown_dimension_placeholders_and_no_implicit_admin(admin,catalog,tmp_path):
    inventory=inspect_indexes(catalog[0],{'notes':'notes_collection'},workspace=tmp_path)
    with pytest.raises(BusinessError) as error:await register(admin,inventory)
    assert error.value.data['reason']=='INDEX_BASELINE_NO_OBSERVED_DIMENSION'
    assert admin.count(t.rag_index_versions)==admin.count(t.ai_version_counters)==admin.count(t.ai_config_requests)==0
    with pytest.raises(BusinessError) as error:await register(admin,inventory,actor=admin.other.user_id)
    assert error.value.code==40312


@pytest.mark.asyncio
async def test_observation_never_unblocks_publication_and_read_status_does_no_io(admin,catalog,tmp_path,monkeypatch):
    await register(admin,inspect_indexes(catalog[0],workspace=tmp_path))
    draft=await admin.draft()
    payload=copy.deepcopy(draft['payload']);payload['role_bindings_raw']['embedding']['enabled']=True
    admin.sql(t.ai_config_drafts.update().values(payload=payload))
    def forbidden(*_args,**_kwargs):raise AssertionError('Status read must not access Chroma')
    monkeypatch.setattr('app.model_config.index_inventory.inspect_indexes',forbidden)
    view=await status(admin,draft_id=draft['id'])
    assert view['draft']['embedding_enabled'] and 'INDEX_BASELINE_NOT_VERIFIED' in view['draft']['publication_blockers']
    with pytest.raises(BusinessError):await publish(admin,draft)
    assert admin.count(t.ai_config_releases)==0


@pytest.mark.asyncio
async def test_status_http_requires_live_grant_and_no_store(api,admin):
    client,_=api
    response=await client.get('/api/v1/model-config/embedding-status')
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert response.json()['data']['registration_status']=='not_registered'
    admin.sql(t.ai_config_admin_grants.update().values(status='revoked'))
    assert (await client.get('/api/v1/model-config/embedding-status')).status_code==403


def test_inspection_cli_is_offline_and_does_not_import_database_sdk_or_factory(catalog,tmp_path,monkeypatch,capsys):
    from app.model_config import cli
    monkeypatch.setattr(sys,'argv',['cli','inspect-indexes','--store',str(catalog[0])])
    original=builtins.__import__
    def guard(name,*args,**kwargs):
        if name in ('app.db.database','app.utils.factory','chromadb'):raise AssertionError('Unexpected online import')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guard)
    cli.main()
    result=json.loads(capsys.readouterr().out)
    assert result['inventory_version']==1 and len(result['collections'])==2
