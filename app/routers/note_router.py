"""
笔记路由

端点：
- POST /note - 创建笔记
- GET /note - 笔记列表（分页）
- GET /note/{note_id} - 笔记详情
- PUT /note/{note_id} - 更新笔记
- DELETE /note/{note_id} - 删除笔记（软删除）
- POST /note/search - 语义搜索
- POST /note/batch - 批量操作
- POST /note/autocomplete - AI 内联补全
- POST /note/write-assistant - AI 写作辅助
- GET /note/recycle-bin - 回收站列表
- POST /note/{note_id}/restore - 恢复笔记
- DELETE /note/{note_id}/permanent - 彻底删除笔记
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.failed_response import BusinessError, ErrorCode
from app.core.success_response import success_response
from app.core.rate_limit import rate_limit
from app.core.model_trace import clear_trace_context, new_request_id, set_trace_context
from app.db.database import get_db
from app.db.redis_client import get_redis
from app.rag.source_access import note_source
from app.schemas.note import (
    NoteCreate, NoteUpdate, NoteResponse, NoteListResponse,
    NoteSearchRequest, NoteSearchResponse, BatchOperation,
    AutocompleteRequest, WriteAssistantRequest,
    DeletedNoteListResponse, DeletedNoteResponse,
)
from app.utils.auth_utils import get_current_user_id
from app.utils.time_utils import utc_now
router = APIRouter()


def _get_note_service():
    """获取 NoteService 实例（从后台初始化管理器获取已注入 vector_store 和 chat_model 的实例）"""
    from main import init_manager
    if init_manager.note_service is not None:
        return init_manager.note_service
    # 后台初始化未完成，返回无向量/模型的基础实例（降级模式）
    from app.services.note_service import NoteService
    return NoteService()


@router.post("/note", summary="创建笔记")
async def create_note(
    data: NoteCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """创建新笔记，自动触发向量双写和异步标签生成"""
    note = await _get_note_service().create_note(db, user_id, data)
    # 显式 commit：调用方紧随其后读取列表，避免 get_db auto-commit
    # 在响应发送后才落地的竞态（与 category_router 写操作同一约定）
    await db.commit()
    return success_response(data=NoteResponse.model_validate(note).model_dump())


@router.get("/note", summary="笔记列表")
async def list_notes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category_id: str = Query(None, description="按分类 ID 过滤（__uncategorized__ = 未分类）"),
    category: str = Query(None, description="按分类字符串过滤（向后兼容）"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """分页获取笔记列表，支持按分类 ID / 分类字符串过滤"""
    notes, total = await _get_note_service().list_notes(
        db, user_id, page, page_size, category, category_id
    )
    return success_response(data=NoteListResponse(
        notes=[NoteResponse.model_validate(n).model_dump() for n in notes],
        total=total, page=page, page_size=page_size,
    ).model_dump())


# ========== 回收站 ==========
# ⚠️ 静态路径必须注册在动态路径（/note/{note_id}）之前，
# 否则 Starlette 按注册顺序匹配，"recycle-bin" 会被 {note_id} 捕获

# 回收站自动清理阈值（天），与 scheduler 定时任务保持一致
RECYCLE_BIN_CLEANUP_DAYS = 14


@router.get("/note/recycle-bin", summary="回收站列表")
async def list_recycle_bin(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """分页获取回收站中的已删除笔记（按删除时间倒序，附带剩余清理天数）"""
    notes, total = await _get_note_service().list_deleted_notes(db, user_id, page, page_size)
    now = utc_now()

    items = []
    for n in notes:
        base = NoteResponse.model_validate(n).model_dump()
        days_elapsed = (now - n.deleted_at).days if n.deleted_at else RECYCLE_BIN_CLEANUP_DAYS
        items.append(DeletedNoteResponse(
            **base, days_remaining=max(0, RECYCLE_BIN_CLEANUP_DAYS - days_elapsed),
        ))

    return success_response(data=DeletedNoteListResponse(
        notes=items, total=total, page=page, page_size=page_size,
    ).model_dump())


@router.get("/note/{note_id}", summary="笔记详情")
async def get_note(
    note_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取单个笔记详情"""
    note = await _get_note_service().get_note(db, note_id, user_id)
    return success_response(data=NoteResponse.model_validate(note).model_dump())


@router.get("/note/{note_id}/index-source", summary="读取已授权的笔记索引版本")
async def get_note_index_source(
    note_id: str, response: Response,
    version: str = Query(pattern=r'^[0-9a-f]{64}$'),
    user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db),
    ordinal: int | None = None,
):
    response.headers['Cache-Control'] = 'private, no-store'
    note, locator = await note_source(db, user_id, note_id, version, ordinal)
    content = (note.content or note.title)[locator['char_start']:locator['char_end']]
    return success_response(data={'source_locator': locator, 'content': content})


@router.put("/note/{note_id}", summary="更新笔记")
async def update_note(
    note_id: str,
    data: NoteUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """更新笔记内容并提交索引任务。"""
    note = await _get_note_service().update_note(db, note_id, user_id, data)
    # 显式 commit（写操作统一约定，见 move_note_to_category 注释）
    await db.commit()
    return success_response(data=NoteResponse.model_validate(note).model_dump())


@router.put("/note/{note_id}/category", summary="移动笔记到分类")
async def move_note_to_category(
    note_id: str,
    category_id: Optional[str] = Query(None, description="目标分类 ID；不传或空表示移为未分类"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """将笔记移动到指定分类（双写期间同步更新 category 字符串）"""
    note = await _get_note_service().move_note_by_id(db, note_id, user_id, category_id)
    # ★ 显式 commit：调用方可能紧随其后读取列表，避免 get_db auto-commit
    # 在响应发送后才落地的竞态（与 category_router 写操作同一约定）
    await db.commit()
    return success_response(data=NoteResponse.model_validate(note).model_dump())


@router.delete("/note/{note_id}", summary="删除笔记")
async def delete_note(
    note_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """软删除笔记（移入回收站）"""
    await _get_note_service().delete_note(db, note_id, user_id)
    # 显式 commit（写操作统一约定，见 move_note_to_category 注释）
    await db.commit()
    return success_response(message="笔记已删除")


# ========== 回收站：恢复 / 彻底删除 ==========


@router.post("/note/{note_id}/restore", summary="恢复笔记")
async def restore_note(
    note_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """从回收站恢复笔记并提交索引重建任务。"""
    await _get_note_service().restore_note(db, note_id, user_id)
    # 显式 commit：前端恢复成功后立即重拉回收站列表，
    # 避免 auto-commit 落库晚于刷新请求（条目"恢复后仍在"的竞态）
    await db.commit()
    return success_response(message="笔记已恢复")


@router.delete("/note/{note_id}/permanent", summary="彻底删除笔记")
async def permanent_delete_note(
    note_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    彻底删除笔记（物理删除 MySQL 记录 + ChromaDB 向量，级联删除回顾记录，不可恢复）

    每用户 30 次/分钟限流（Redis 计数器手动实现，rate_limit 装饰器不支持自定义窗口）。
    """
    # 每用户 30 次/分钟 限流
    redis = get_redis()
    key = f"perm_delete:{user_id}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    if count > 30:
        raise BusinessError(
            code=ErrorCode.ENDPOINT_RATE_LIMIT,
            message="操作过于频繁，请稍后再试",
            http_status=429,
        )

    await _get_note_service().permanent_delete(db, note_id, user_id)
    # 显式 commit：前端彻底删除成功后立即重拉回收站列表，
    # 避免 auto-commit 落库晚于刷新请求（条目"删除后仍在"的竞态——本 bug 根因）
    await db.commit()
    return success_response(message="笔记已彻底删除")


@router.post("/note/search", summary="关键词搜索")
async def search_notes(
    data: NoteSearchRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """基于 MySQL 的标题/正文关键词搜索（LIKE 模糊匹配）

    不依赖外部 Embedding 服务，稳定零外部依赖；响应格式保持
    {query, results: [{note, score}]}，score 仅作排序权重（标题命中 1.0 > 正文命中 0.5）。
    语义搜索仍保留给 AI 对话的 search_notes_tool 使用（note_service.semantic_search）。
    """
    results = await _get_note_service().keyword_search(db, user_id, data.query, data.top_k)
    return success_response(data={
        "query": data.query,
        "results": [
            {"note": NoteResponse.model_validate(n).model_dump(), "score": s}
            for n, s in results
        ]
    })


@router.post("/note/batch", summary="批量操作")
async def batch_operation(
    data: BatchOperation,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    批量操作笔记（删除/置顶/取消置顶/移动分类/恢复/彻底删除）

    使用显式 commit 确保变更在响应返回前已持久化到 MySQL。
    不依赖 get_db() 的 auto-commit，避免 cleanup 阶段异常导致变更丢失。
    """
    from app.core.logger_handler import logger
    note_service = _get_note_service()

    success_ids = []
    errors = []

    for note_id in data.note_ids:
        try:
            async with db.begin_nested():
                if data.operation == 'delete':
                    await note_service.delete_note(db, note_id, user_id)
                elif data.operation == 'pin':
                    await note_service.pin_note(db, note_id, user_id)
                elif data.operation == 'unpin':
                    await note_service.unpin_note(db, note_id, user_id)
                elif data.operation == 'move':
                    if data.target_category_id:
                        await note_service.move_note_by_id(db, note_id, user_id, data.target_category_id)
                    else:
                        await note_service.move_note(db, note_id, user_id, data.target_category)
                elif data.operation == 'permanent_delete':
                    await note_service.permanent_delete(db, note_id, user_id)
                elif data.operation == 'restore':
                    await note_service.restore_note(db, note_id, user_id)
                else:
                    raise ValueError(f"未知的操作类型: {data.operation}")
            success_ids.append(note_id)
        except Exception as e:
            errors.append({"note_id": note_id, "error": str(e)})
            logger.warning(f"批量操作单条失败: note_id={note_id}, error={e}")

    # ★ 显式 commit：确保变更在响应返回前持久化到 MySQL
    # commit 后 SQLAlchemy autobegin 开启新事务，get_db() 的后续 commit 是空操作
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"批量操作事务提交失败: operation={data.operation}, error={e}")
        from app.core.failed_response import BusinessError, ErrorCode
        raise BusinessError(
            code=ErrorCode.INTERNAL_ERROR, http_status=500,
            message=f"批量操作提交失败: {e}"
        )

    logger.info(
        f"批量操作完成: operation={data.operation}, "
        f"success={len(success_ids)}, errors={len(errors)}"
    )

    return success_response(data={
        "operation": data.operation,
        "total": len(data.note_ids),
        "success_count": len(success_ids),
        "error_count": len(errors),
        "errors": errors if errors else None,
    }, message=f"批量{data.operation}操作完成：成功 {len(success_ids)} 篇，失败 {len(errors)} 篇")


@router.post("/note/autocomplete", summary="AI 内联补全")
async def autocomplete(
    data: AutocompleteRequest,
    user_id: str = Depends(get_current_user_id),      # ★ C1：必须排在 rate_limit 之前
    _rl=Depends(rate_limit(endpoint_limit=10)),       # ★ C1：参数顺序写法，限流才能读到 user_id
):
    """AI 内联补全（根据上下文续写）"""
    # ★ 修复（评审 F17）：note 类端点此前从不设置 trace 上下文，而
    # set_trace_stage 在无上下文时是空操作 → model_traces.user_id 落为空串、
    # stage 落为默认 "chat"，导致按用户聚合的用量/费用看不到补全调用。
    # 必须先 set_trace_context 再调用模型。
    set_trace_context(new_request_id(), user_id=user_id, stage="autocomplete")
    try:
        result = await _get_note_service().ai_autocomplete(
            content=data.content,
            cursor_position=data.cursor_position,
            title=data.title or "",
        )
    finally:
        clear_trace_context()
    return success_response(data=result)


@router.post("/note/write-assistant", summary="AI 写作辅助")
async def write_assistant(
    data: WriteAssistantRequest,
    user_id: str = Depends(get_current_user_id),      # ★ 必须排在 rate_limit 之前
    _rl=Depends(rate_limit(endpoint_limit=10)),       # ★ 参数顺序写法（C1）
):
    """AI 写作辅助（续写 / 扩写 / 摘要）"""
    # 与补全一致：note 端点需显式建 trace 上下文，否则费用归不到用户名下（F17）
    set_trace_context(new_request_id(), user_id=user_id, stage="write_assistant")
    try:
        result = await _get_note_service().ai_write_assist(
            content=data.content,
            mode=data.mode,
            title=data.title or "",
        )
    finally:
        clear_trace_context()
    return success_response(data=result)
