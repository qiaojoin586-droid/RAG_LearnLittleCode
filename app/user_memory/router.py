"""Personal memory API: ownership comes only from the verified login identity."""
from fastapi import APIRouter,Depends,Body,Header,Query,Response,Request
from fastapi.responses import Response as RawResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.rate_limit import rate_limit
from app.core.success_response import success_response
from app.db.database import async_session_factory
from app.user_memory.service import MemoryService,etag,error
from app.utils.auth_utils import get_current_user_id
from app.user_memory.jobs import MemoryJobs

service=MemoryService()


async def private_headers(response:Response):response.headers['Cache-Control']='private, no-store'


class MemoryPrivacyMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        async def private_send(message):
            if message['type']=='http.response.start':
                from starlette.datastructures import MutableHeaders
                MutableHeaders(scope=message)['Cache-Control']='private, no-store'
            await send(message)
        await self.app(scope,receive,private_send if scope.get('path','').startswith('/api/v1/user/memory') else send)


async def memory_db():
    try:
        async with async_session_factory() as db:
            try:yield db
            finally:await db.rollback()
    except SQLAlchemyError:error(50340,'MEMORY_STORE_UNAVAILABLE',503)


router=APIRouter(prefix='/user/memory',tags=['Personal Memory'],dependencies=[Depends(private_headers)])


@router.get('/jobs')
async def jobs(limit:int=Query(20,ge=1,le=100),cursor:str|None=Query(None,max_length=36),
    user_id=Depends(get_current_user_id),db=Depends(memory_db)):
    return success_response(await MemoryJobs(service).list(db,user_id,limit,cursor))


@router.get('/jobs/{job_id}')
async def job(job_id:str,user_id=Depends(get_current_user_id),db=Depends(memory_db)):
    return success_response(await MemoryJobs(service).get_job(db,user_id,job_id))


@router.post('/generate')
async def generate(response:Response,request:Request,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    # Separate ten-minute admission budget; proofs/passwords are not involved.
    count=await request.app.state.redis.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],600) end; return n",1,'memory_generate:'+user_id)
    if int(count)>3:
        from app.core.failed_response import BusinessError
        raise BusinessError(42902,http_status=429)
    value=await MemoryJobs(service).generate(db,user_id,body,key,match)
    response.status_code=202 if value['status'] in ('queued','running') else 200
    return success_response(value)


@router.post('/jobs/{job_id}/apply')
async def apply(job_id:str,response:Response,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    return document(response,await MemoryJobs(service).proposal(db,user_id,job_id,body,key,match))


@router.post('/jobs/{job_id}/dismiss')
async def dismiss(job_id:str,response:Response,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    return document(response,await MemoryJobs(service).proposal(db,user_id,job_id,body,key,match,dismiss=True))


def document(response,value):
    response.headers['ETag']=etag(value['head_version'])
    return success_response(value)


@router.get('')
async def get(response:Response,user_id=Depends(get_current_user_id),db=Depends(memory_db)):
    return document(response,await service.get(db,user_id))


@router.put('')
async def save(response:Response,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    return document(response,await service.save(db,user_id,body,key,match))


@router.patch('/settings')
async def settings(response:Response,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    return document(response,await service.settings(db,user_id,body,key,match))


@router.get('/revisions')
async def revisions(limit:int=Query(20,ge=1,le=100),cursor:str|None=Query(None,max_length=36),
    user_id=Depends(get_current_user_id),db=Depends(memory_db)):
    return success_response(await service.revisions(db,user_id,limit,cursor))


@router.get('/revisions/{revision_id}')
async def revision(revision_id:str,user_id=Depends(get_current_user_id),db=Depends(memory_db)):
    await service.authorize(db,user_id)
    from app.model_config.service import json_view
    return success_response(json_view(await service.revision(db,user_id,revision_id)))


@router.post('/revisions/{revision_id}/restore')
async def restore(revision_id:str,response:Response,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    return document(response,await service.restore(db,user_id,revision_id,body,key,match))


@router.post('/clear')
async def clear(response:Response,body:dict=Body(...),user_id=Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=30)),db=Depends(memory_db),
    key:str|None=Header(None,alias='Idempotency-Key',max_length=128),
    match:str|None=Header(None,alias='If-Match',max_length=64)):
    return document(response,await service.clear(db,user_id,body,key,match))


@router.get('/download')
async def download(user_id=Depends(get_current_user_id),db=Depends(memory_db)):
    value=await service.get(db,user_id)
    if not value['exists']:error(40440,'MEMORY_NOT_FOUND',404)
    return RawResponse(value['content_md'].encode('utf-8'),media_type='text/markdown; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename="memory.md"','Cache-Control':'private, no-store',
                 'ETag':etag(value['head_version'])})
