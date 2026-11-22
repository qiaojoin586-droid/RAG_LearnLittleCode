"""
笔记核心业务服务

实现笔记的完整生命周期管理：
- CRUD 操作 + ChromaDB 向量双写
- 异步自动标签生成（LLM）
- 语义搜索（ChromaDB 向量检索 → MySQL 回填）
- 关联推荐（双源检索）
- AI 内联补全 / 写作辅助
- 批量操作
- Markdown 导出
"""
from app.model_config.runtime import model_for, template_for
from app.model_config.snapshots import current_snapshot

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import partial

from typing import List, Optional, Tuple

from sqlalchemy import select, update, func, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, ErrorCode
from app.core.task_runner import spawn_background_task
from app.models.note import Note
from app.models.category import NoteCategory
from app.models.review import ReviewRecord
from app.rag.note_index import load_visible_notes, candidate_note_locator, queue_note_index
from app.schemas.note import NoteCreate, NoteUpdate
from app.utils.time_utils import utc_now


# "未分类"过滤哨兵值（与前端保持一致）：
# GET /note?category_id=__uncategorized__ → WHERE category_id IS NULL
UNCATEGORIZED_SENTINEL = "__uncategorized__"

# AI 联机补全后处理的引号集合（半角 + 中文弯引号）# ⚠️ 必须放在模块级：行内写成 ('"', "'", '"', ''') 会被 Python 解析为三引号字符串，
#    吞掉后续的 `and text[-1] in (...)`，导致"只剥首字符"的静默逻辑错误
#    （设计文档 §3.6 的 C3，AST 实测可复现）。
# F11：按"左引号 → 右引号"映射判定，必须首尾成对才剥离（避免 `"abc'` 被误剥）。
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}
# 兼容：全部引号字符（含右引号），供测试与外部引用
_QUOTE_CHARS = frozenset('"\'' + '“”‘’')

# AI 写作辅助模式 → 提示词中的模式说明
_WRITE_MODES = {
    "continue": "续写——紧接笔记末尾继续写，保持风格与主题一致，写 2-4 句",
    "expand": "扩写——在保留原意的前提下把内容写得更详细具体，可补充细节或例子",
    "summary": "摘要——用 2-3 句话概括笔记的核心内容",
}


class NoteService:
    """
    笔记服务类
    
    核心设计：
    - 向量索引：笔记与索引任务同事务提交，由 Worker 写入 ChromaDB
    - 异步自动标签：保存后立即返回，后台调用 LLM 生成标签和分类
    - 语义搜索：ChromaDB 检索 → 提取 note_id → MySQL 回填完整数据
    """
    
    def __init__(self, vector_store=None, chat_model=None, autocomplete_model=None):
        """
        初始化笔记服务
        
        Args:
            vector_store: 向量存储服务实例（ChromaDB）
            chat_model: LLM Chat 模型实例（用于自动标签生成）
            autocomplete_model: AI 联机补全专用模型（轻量，None = 功能关闭）
        """
        self.vector_store = vector_store
        self.chat_model = chat_model
        self.autocomplete_model = autocomplete_model
    
    async def create_note(
        self, db: AsyncSession, user_id: str, note_data: NoteCreate, *,
        after_commit: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> Note:
        """
        创建笔记
        
        流程：
        1. 写入 MySQL
        2. 在同一事务写入索引任务和回顾记录
        3. 后台异步生成标签
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            note_data: 笔记创建数据
            
        Returns:
            创建的笔记对象
        """
        note = Note(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=note_data.title,
            content=note_data.content,
            tags=note_data.tags or [],
            category=note_data.category,
            is_pinned=note_data.is_pinned,
            is_indexed=note_data.is_indexed,
            # 文档类型（md/txt）：仅新建时写入；AI 工具不传时默认 'md'
            format=note_data.format or 'md',
        )

        # 双写：category_id 优先，同时将 category 字符串同步为分类名
        if note_data.category_id:
            cat = await self._resolve_category_by_id(db, user_id, note_data.category_id)
            note.category_id = cat.id
            note.category = cat.name
        
        db.add(note)
        await db.flush()
        
        # 刷新对象以加载数据库生成的字段（created_at, updated_at 等 server_default 值）
        await db.refresh(note)

        # A review is durable business data: commit it with the note, independently
        # of optional AI tagging or admission to the background queue.
        db.add(ReviewRecord(note_id=note.id, user_id=user_id, review_count=0,
                            interval_days=1, next_review_at=func.now()))
        await db.flush()
        if note.is_indexed:
            await queue_note_index(db, note, 'upsert')
        
        if after_commit is None:
            await self._schedule_created_note(note)
        else:
            after_commit.append(partial(self._schedule_created_note, note))
        logger.info(f"笔记创建成功: note_id={note.id}, user_id={user_id}")
        return note

    async def _schedule_created_note(self, note: Note) -> None:
        task = spawn_background_task(self._auto_tag_note(note), name="note_auto_tag")
        if task is None:
            logger.warning("可选标签任务因容量不足跳过；回顾记录随笔记事务保存")
    
    async def get_note(self, db: AsyncSession, note_id: str, user_id: str, *, lock: bool = False) -> Note:
        """
        获取单个笔记（带权限校验）
        
        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 当前用户 ID
            
        Returns:
            笔记对象
            
        Raises:
            BusinessError: 笔记不存在或无权访问
        """
        statement = select(Note).where(
            Note.id == note_id, Note.user_id == user_id, Note.deleted_at.is_(None),
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        result = await db.execute(statement)
        note = result.scalar_one_or_none()
        
        if not note:
            raise BusinessError(code=ErrorCode.NOTE_NOT_FOUND, http_status=404)

        return note

    async def get_notes_by_ids(
        self, db: AsyncSession, note_ids: List[str], user_id: str
    ) -> List[Note]:
        """
        批量获取笔记（权限校验 + 已删除过滤，设计方案 §9.1）

        与 get_note 不同：不抛 BusinessError —— 部分 ID 无效/不存在/无权限时
        静默忽略，只返回「属于该用户且未删除」的有效子集，调用方按需提示。

        保持输入顺序：SQL `IN()` 不保证返回顺序与输入一致（MySQL 通常按
        主键索引顺序返回），必须在代码内按输入顺序重排——否则生成 PPT 时
        章节顺序会与用户选择顺序不符。

        Args:
            db: 数据库会话
            note_ids: 笔记 ID 列表
            user_id: 当前用户 ID

        Returns:
            有效笔记列表（按 note_ids 输入顺序排列）
        """
        if not note_ids:
            return []

        result = await db.execute(
            select(Note).where(
                and_(
                    Note.id.in_(note_ids),
                    Note.user_id == user_id,
                    Note.deleted_at.is_(None),
                )
            )
        )
        notes = list(result.scalars().all())
        id_order = {nid: i for i, nid in enumerate(note_ids)}
        notes.sort(key=lambda n: id_order.get(n.id, 999))   # 理论上不会出现未匹配项，兜底排最后
        return notes

    async def list_notes(
        self, db: AsyncSession, user_id: str,
        page: int = 1, page_size: int = 20,
        category: Optional[str] = None,
        category_id: Optional[str] = None,
    ) -> Tuple[List[Note], int]:
        """
        分页获取笔记列表

        Args:
            db: 数据库会话
            user_id: 用户 ID
            page: 页码
            page_size: 每页数量
            category: 按分类字符串过滤（可选，双写期间向后兼容）
            category_id: 按分类 ID 过滤（可选，优先；哨兵值 __uncategorized__ = 未分类）

        Returns:
            (笔记列表, 总数)
        """
        # 构建查询
        query = select(Note).where(
            and_(Note.user_id == user_id, Note.deleted_at.is_(None))
        )
        count_query = select(func.count()).select_from(Note).where(
            and_(Note.user_id == user_id, Note.deleted_at.is_(None))
        )

        if category_id:
            if category_id == UNCATEGORIZED_SENTINEL:
                # "未分类"哨兵值 → category_id IS NULL
                query = query.where(Note.category_id.is_(None))
                count_query = count_query.where(Note.category_id.is_(None))
            else:
                query = query.where(Note.category_id == category_id)
                count_query = count_query.where(Note.category_id == category_id)
        elif category:
            # 降级到字符串过滤（向后兼容；原"其他" not_in 兜底逻辑已随迁移移除）
            query = query.where(Note.category == category)
            count_query = count_query.where(Note.category == category)
        
        # 排序：置顶优先，然后按更新时间倒序
        query = query.order_by(Note.is_pinned.desc(), Note.updated_at.desc())
        
        # 分页
        query = query.offset((page - 1) * page_size).limit(page_size)
        
        result = await db.execute(query)
        total_result = await db.execute(count_query)
        
        notes = list(result.scalars().all())
        total = total_result.scalar()
        
        return notes, total
    
    async def update_note(
        self, db: AsyncSession, note_id: str, user_id: str, note_data: NoteUpdate,
    ) -> Note:
        """
        更新笔记
        
        标题或正文变更时提交新的索引任务。
        
        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID
            note_data: 更新数据
            
        Returns:
            更新后的笔记对象
        """
        note = await self.get_note(db, note_id, user_id, lock=True)

        # 更新字段
        update_data = note_data.model_dump(exclude_unset=True)
        if "category_id" in update_data:
            # 双写：category_id 优先；显式传 None 表示移为未分类，同步清空字符串
            if update_data["category_id"]:
                cat = await self._resolve_category_by_id(db, user_id, update_data["category_id"])
                update_data["category_id"] = cat.id
                update_data["category"] = cat.name
            elif "category" not in update_data:
                # 显式 None 且未同时提供字符串 → 清空两字段；
                # 若同时提供了字符串（AI 工具未匹配分类的降级场景），保留字符串
                update_data["category"] = None
        eligibility_changed = 'is_indexed' in update_data and update_data['is_indexed'] != note.is_indexed
        reindex = any(field in update_data and update_data[field] != getattr(note, field)
                      for field in ('title', 'content'))
        for field, value in update_data.items():
            setattr(note, field, value)
        
        note.updated_at = func.now()
        await db.flush()
        # 回读数据库生成的 updated_at（func.now() 是 SQL 表达式，flush 后属性待回读；
        # 不 refresh 的话，后续响应序列化时在 greenlet 外触发 SELECT → MissingGreenlet）
        await db.refresh(note)

        if eligibility_changed or (reindex and note.is_indexed):
            await queue_note_index(db, note, 'upsert' if note.is_indexed else 'delete')
        logger.info(f"笔记更新: note_id={note_id}")
        return note
    
    async def delete_note(self, db: AsyncSession, note_id: str, user_id: str) -> None:
        """
        软删除笔记（设置 deleted_at）

        使用直接 SQL UPDATE 而非 ORM 对象修改，确保变更可靠持久化。
        同时记录持久的向量清理任务。

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID
        """
        # 先验证笔记存在（权限校验 + 确保未删除）
        note = await self.get_note(db, note_id, user_id, lock=True)

        # 直接 SQL UPDATE —— 绕过 ORM flush 的对象状态跟踪，
        # 确保 UPDATE 语句一定被发送到数据库连接
        result = await db.execute(
            update(Note)
            .where(and_(Note.id == note_id, Note.user_id == user_id))
            .values(deleted_at=func.now())   # 设置 deleted_at 为当前时间戳 使用数据库服务时间与其他事件字段保持一致
        )

        if result.rowcount == 0:
            raise BusinessError(code=ErrorCode.NOTE_NOT_FOUND, http_status=404)

        await queue_note_index(db, note, 'delete')

        logger.info(f"笔记软删除: note_id={note_id}")

    # ========== 回收站 ==========

    async def list_deleted_notes(
        self, db: AsyncSession, user_id: str, page: int = 1, page_size: int = 20
    ) -> Tuple[List[Note], int]:
        """
        分页获取回收站笔记列表（deleted_at IS NOT NULL，按删除时间倒序）

        Args:
            db: 数据库会话
            user_id: 用户 ID
            page: 页码
            page_size: 每页数量

        Returns:
            (回收站笔记列表, 总数)
        """
        query = select(Note).where(
            and_(Note.user_id == user_id, Note.deleted_at.isnot(None))
        ).order_by(Note.deleted_at.desc())
        count_query = select(func.count()).select_from(Note).where(
            and_(Note.user_id == user_id, Note.deleted_at.isnot(None))
        )

        query = query.offset((page - 1) * page_size).limit(page_size)

        result = await db.execute(query)
        total_result = await db.execute(count_query)

        notes = list(result.scalars().all())
        total = total_result.scalar()

        return notes, total

    async def restore_note(self, db: AsyncSession, note_id: str, user_id: str) -> Note:
        """
        从回收站恢复笔记并提交索引重建任务。

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID

        Returns:
            恢复后的笔记对象

        Raises:
            BusinessError: 笔记不存在、无权访问或不在回收站中
        """
        # 1. 查询笔记（验证归属 + 确保在回收站中）
        result = await db.execute(
            select(Note).where(
                and_(Note.id == note_id, Note.user_id == user_id, Note.deleted_at.isnot(None))
            ).with_for_update().execution_options(populate_existing=True)
        )
        note = result.scalar_one_or_none()
        if not note:
            raise BusinessError(code=ErrorCode.NOTE_NOT_FOUND, http_status=404)

        # 2. 清除 deleted_at
        note.deleted_at = None
        note.updated_at = func.now()
        await db.flush()
        # 回读数据库生成的 updated_at（func.now() 为 SQL 表达式，见 update_note 注释）
        await db.refresh(note)

        if note.is_indexed:
            await queue_note_index(db, note, 'upsert')

        logger.info(f"笔记恢复: note_id={note_id}")
        return note

    async def permanent_delete(self, db: AsyncSession, note_id: str, user_id: str) -> None:
        """
        彻底删除笔记（物理删除 MySQL 记录 + 删除 ChromaDB 向量）

        ⚠️ 级联影响：Note.review_records 关系定义了 cascade="all, delete-orphan"
        （app/models/note.py），物理删除 Note 时 SQLAlchemy 会级联删除该笔记的
        所有 ReviewRecord（回顾记录），且不可恢复。前端确认弹窗需明确提示。

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID

        Raises:
            BusinessError: 笔记不存在、无权访问或不在回收站中
        """
        # 1. 验证笔记属于当前用户 且已删除（回收站内）
        result = await db.execute(
            select(Note).where(
                and_(Note.id == note_id, Note.user_id == user_id, Note.deleted_at.isnot(None))
            ).with_for_update().execution_options(populate_existing=True)
        )
        note = result.scalar_one_or_none()
        if not note:
            raise BusinessError(code=ErrorCode.NOTE_NOT_FOUND, http_status=404)

        await queue_note_index(db, note, 'delete')

        # 3. 物理删除（ORM 级联删除 review_records）
        await db.delete(note)
        await db.flush()

        logger.info(f"笔记彻底删除: note_id={note_id}")

    async def cleanup_expired_notes(self, db: AsyncSession, days: int = 14) -> int:
        """
        清理过期笔记（deleted_at 超过 days 天）→ 返回删除数量

        定时任务调用；同时清理 ChromaDB 向量，并级联删除 review_records。

        Args:
            db: 数据库会话
            days: 过期阈值天数（默认 14 天）

        Returns:
            本次物理删除的笔记数量
        """
        from datetime import timedelta

        cutoff = utc_now() - timedelta(days=days)

        result = await db.execute(
            select(Note).where(
                and_(Note.deleted_at.isnot(None), Note.deleted_at < cutoff)
            ).order_by(Note.id).with_for_update().execution_options(populate_existing=True)
        )
        expired = list(result.scalars().all())
        if not expired:
            return 0

        # 物理删除（ORM 级联删除 review_records）
        for note in expired:
            await queue_note_index(db, note, 'delete')
            await db.delete(note)
        await db.flush()

        logger.info(f"清理过期笔记完成: 删除 {len(expired)} 条（超过 {days} 天）")
        return len(expired)

    async def pin_note(self, db: AsyncSession, note_id: str, user_id: str) -> Note:
        """
        置顶笔记

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID

        Returns:
            更新后的笔记对象
        """
        note = await self.get_note(db, note_id, user_id)
        note.is_pinned = True
        note.updated_at = func.now()
        await db.flush()
        await db.refresh(note)  # 回读 updated_at（func.now() 为 SQL 表达式，见 update_note 注释）
        logger.info(f"笔记置顶: note_id={note_id}")
        return note

    async def unpin_note(self, db: AsyncSession, note_id: str, user_id: str) -> Note:
        """
        取消置顶笔记

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID

        Returns:
            更新后的笔记对象
        """
        note = await self.get_note(db, note_id, user_id)
        note.is_pinned = False
        note.updated_at = func.now()
        await db.flush()
        await db.refresh(note)  # 回读 updated_at（func.now() 为 SQL 表达式，见 update_note 注释）
        logger.info(f"笔记取消置顶: note_id={note_id}")
        return note

    async def move_note(
        self, db: AsyncSession, note_id: str, user_id: str, category: str
    ) -> Note:
        """
        移动笔记到目标分类

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID
            category: 目标分类（必填，已在 schema 层校验）

        Returns:
            更新后的笔记对象
        """
        note = await self.get_note(db, note_id, user_id)
        note.category = category
        note.updated_at = func.now()
        await db.flush()
        await db.refresh(note)  # 回读 updated_at（func.now() 为 SQL 表达式，见 update_note 注释）
        logger.info(f"笔记移动分类: note_id={note_id}, category={category}")
        return note

    async def _resolve_category_by_id(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> NoteCategory:
        """按 ID 解析分类（归属校验 + 排除软删除，未找到抛 404）"""
        result = await db.execute(
            select(NoteCategory).where(
                NoteCategory.id == category_id,
                NoteCategory.user_id == user_id,
                NoteCategory.deleted_at.is_(None),
            )
        )
        cat = result.scalar_one_or_none()
        if not cat:
            raise BusinessError(code=ErrorCode.CATEGORY_NOT_FOUND, http_status=404)
        return cat

    async def move_note_by_id(
        self, db: AsyncSession, note_id: str, user_id: str, category_id: Optional[str]
    ) -> Note:
        """
        按分类 ID 移动笔记（双写：category 字符串同步更新）

        Args:
            db: 数据库会话
            note_id: 笔记 ID
            user_id: 用户 ID
            category_id: 目标分类 ID；None 表示移为"未分类"（双字段同步清空）

        Returns:
            更新后的笔记对象
        """
        note = await self.get_note(db, note_id, user_id)

        if category_id:
            cat = await self._resolve_category_by_id(db, user_id, category_id)
            note.category_id = cat.id
            note.category = cat.name
        else:
            note.category_id = None
            note.category = None

        note.updated_at = func.now()
        await db.flush()
        await db.refresh(note)  # 回读 updated_at（func.now() 为 SQL 表达式，见 update_note 注释）
        logger.info(f"笔记移动分类: note_id={note_id}, category_id={category_id}")
        return note

    async def semantic_search(
        self, db: AsyncSession, user_id: str, query: str, top_k: int = 5
    ) -> List[Tuple[Note, float]]:
        """
        语义搜索笔记
        
        流程：
        1. ChromaDB 向量检索（按 user_id 过滤）
        2. 提取 note_id 列表
        3. MySQL 批量查询完整笔记数据
        4. 按相似度分数排序返回
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            query: 搜索关键词
            top_k: 返回结果数量
            
        Returns:
            [(Note, score), ...] 按相似度降序排列
        """
        if not self.vector_store or top_k < 1:
            return []

        limit = top_k
        while True:
            results = await self.vector_store.search_notes(query=query, user_id=user_id, top_k=limit)
            notes = await load_visible_notes(db, user_id, [entry['note_id'] for entry in results])
            best = {}
            duplicate = False
            for entry in results:
                row = notes.get(entry['note_id'])
                if not candidate_note_locator(entry, row):
                    continue
                key = row[0].id
                if key in best:
                    duplicate = True
                if key not in best or entry['score'] > best[key][1]:
                    best[key] = (row[0], entry['score'])
            if len(best) >= top_k or len(results) < limit or not duplicate:
                return sorted(best.values(), key=lambda entry: entry[1], reverse=True)[:top_k]
            # A long note can occupy the entire first page of vector hits.
            limit *= 2

    async def keyword_search(
        self, db: AsyncSession, user_id: str, query: str, top_k: int = 5
    ) -> List[Tuple[Note, float]]:
        """
        关键词搜索笔记（MySQL FULLTEXT 优先 + LIKE 兜底，不依赖外部 Embedding 服务）

        策略：
        1. 关键词 ≥2 字符 → MATCH AGAINST（ngram 双字分词，相关度 + 更新时间排序）
        2. 单字符 / FULLTEXT 无命中 / 索引缺失等异常 → LIKE 字面匹配兜底
        score 仅作排序权重（标题命中 1.0 > 正文命中 0.5；FULLTEXT 相关度命中 0.9），
        非语义相似度。语义搜索（semantic_search）仍保留给 AI 对话的 search_notes_tool 使用。

        Args:
            db: 数据库会话
            user_id: 用户 ID
            query: 搜索关键词
            top_k: 返回结果数量

        Returns:
            [(Note, score), ...] 按权重降序排列
        """
        keyword = (query or "").strip()
        if not keyword:
            return []

        # ngram 分词最小 token 为 2 字符：单字符关键词直接走 LIKE
        if len(keyword) < 2:
            return await self._like_search(db, user_id, keyword, top_k)

        # FULLTEXT 优先：MATCH AGAINST 自然语言模式（ngram 支持中文/英文子串）
        # 注意：SQLAlchemy 的 func.match().against() 不是合法构造（Function 无 against），
        # 用 text() 绑定参数实现（表名固定为 notes；关键词经 execute 参数绑定，防注入）
        try:
            match_where = text(
                "MATCH (notes.title, notes.content) AGAINST (:kw IN NATURAL LANGUAGE MODE)"
            )
            match_order = text(
                "MATCH (notes.title, notes.content) AGAINST (:kw IN NATURAL LANGUAGE MODE) DESC"
            )
            result = await db.execute(
                select(Note)
                .where(
                    and_(
                        Note.user_id == user_id,
                        Note.deleted_at.is_(None),
                        match_where,
                    )
                )
                .order_by(match_order, Note.updated_at.desc())
                .limit(top_k),
                {"kw": keyword},
            )
            notes = list(result.scalars().all())
            if notes:
                return [(n, 0.9) for n in notes]
        except Exception as e:
            # FULLTEXT 索引缺失/方言不支持等异常 → LIKE 兜底，保证搜索始终可用
            logger.warning(f"FULLTEXT 查询失败，回退 LIKE: {type(e).__name__}: {e}")

        # LIKE 兜底：FULLTEXT 无命中（符号/罕见词）时保证字面匹配
        return await self._like_search(db, user_id, keyword, top_k)

    async def _like_search(
        self, db: AsyncSession, user_id: str, keyword: str, top_k: int
    ) -> List[Tuple[Note, float]]:
        """
        LIKE 字面匹配兜底（标题命中优先于正文命中，同类内按更新时间倒序）
        """
        # 转义 LIKE 通配符（% 与 _ 按字面匹配）
        escaped = (
            keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"

        # 1) 标题命中（优先级高）
        title_result = await db.execute(
            select(Note)
            .where(
                and_(
                    Note.user_id == user_id,
                    Note.deleted_at.is_(None),
                    Note.title.like(pattern, escape="\\"),
                )
            )
            .order_by(Note.updated_at.desc())
            .limit(top_k)
        )
        title_notes = list(title_result.scalars().all())
        results: List[Tuple[Note, float]] = [(n, 1.0) for n in title_notes]

        # 2) 正文命中补齐（排除已命中的标题笔记）
        remaining = top_k - len(title_notes)
        if remaining > 0:
            conds = [
                Note.user_id == user_id,
                Note.deleted_at.is_(None),
                Note.content.like(pattern, escape="\\"),
            ]
            if title_notes:
                conds.append(Note.id.not_in([n.id for n in title_notes]))
            content_result = await db.execute(
                select(Note)
                .where(and_(*conds))
                .order_by(Note.updated_at.desc())
                .limit(remaining)
            )
            results.extend((n, 0.5) for n in content_result.scalars().all())

        return results

    async def _auto_tag_note(self, note: Note) -> None:
        """
        可选后台自动标签；回顾记录在 create_note 的数据库事务内创建。
        
        使用独立的数据库会话，避免依赖已关闭的请求 session。
        
        Args:
            note: 笔记对象
        """
        try:
            # 1. AI 自动标签（失败/超时静默跳过，标签是增强不是关卡）
            tags = await self._generate_auto_tags(note)
            if tags:
                await self._persist_note_tags(note.id, tags)

            logger.info(f"自动标签完成: note_id={note.id}, tags={tags}")
            
        except Exception as e:
            logger.error(f"自动标签任务失败: note_id={note.id}, error={e}")

    async def _generate_auto_tags(self, note: Note) -> list:
        """
        调用 LLM 生成标签（**不写库**，返回标签列表；任何跳过/失败返回 []）

        跳过条件：
        - `AUTO_TAG_ENABLED=false`
        - 未注入 chat_model
        - 笔记已有标签（不覆盖用户手工标签）
        - 正文过短（< 50 字符，多为草稿/占位）

        设计要点：
        - 正文截断至 `AUTO_TAG_MAX_CONTENT_CHARS`（默认 1200），控制成本
        - 后台任务无请求上下文 → 显式 set_trace_context(user_id)，否则
          model_traces.user_id 为空、按用户聚合的费用看不到这次调用
        - 提示词要求"分类类别全部设置为未分类"，故**只取 tags、忽略 category**
        """
        role_model = model_for('auto_tag', self.chat_model)
        from app.core.model_trace import (
            clear_trace_context, new_request_id, set_trace_context,
        )
        from app.utils.prompt_loader import format_prompt

        if current_snapshot() is None and os.getenv("AUTO_TAG_ENABLED", "true").strip().lower() != "true":
            return []
        if role_model is None:
            return []
        if note.tags:  # 用户已设置标签 → 不覆盖
            return []

        content = (note.content or "").strip()
        if len(content) < 50:
            return []

        max_chars = int(os.getenv("AUTO_TAG_MAX_CONTENT_CHARS", "1200"))
        try:
            prompt = format_prompt(
                "auto_tag",
                note_title=note.title or "无标题",
                note_content=content[:max_chars],
            )
        except (FileNotFoundError, KeyError) as e:
            logger.warning(f"自动标签 prompt 加载失败: {e}")
            return []

        set_trace_context(new_request_id(), user_id=note.user_id, stage="auto_tag")
        try:
            async with asyncio.timeout(30):
                response = await role_model.ainvoke([
                    ("system", "你是笔记标签助手，只输出 JSON，不要任何解释。"),
                    ("user", prompt),
                ])
            raw = response.content if isinstance(response.content, str) else ""
        except Exception as e:
            logger.warning(f"自动标签调用失败: note_id={note.id}, {type(e).__name__}: {e}")
            from app.model_config.runtime import require_failure_policy
            require_failure_policy('auto_tag','disabled')
            return []
        finally:
            clear_trace_context()

        return self._parse_auto_tags(raw)

    @staticmethod
    def _parse_auto_tags(raw: str) -> list:
        """
        解析 LLM 返回的标签 JSON（纯函数，容错优先）

        容错：剥 ``` 代码围栏 → 直接 json.loads；失败则取首个 {...} 再试；
        过滤非字符串/空白/超长（> 20 字符）标签，去重，最多 5 个。
        """
        text = re.sub(r"```(?:json)?\s*", "", raw or "").replace("```", "").strip()
        if not text:
            return []

        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return []

        if not isinstance(data, dict):
            return []
        raw_tags = data.get("tags")
        if not isinstance(raw_tags, list):
            return []

        tags: list = []
        for item in raw_tags:
            if not isinstance(item, str):
                continue
            tag = item.strip().strip('"').strip()
            if not tag or len(tag) > 20 or tag in tags:
                continue
            tags.append(tag)
        return tags[:5]

    async def _persist_note_tags(self, note_id: str, tags: list) -> None:
        """把生成的标签写回 notes 表（独立会话 + 直接 UPDATE，避免用已关闭 session 的 ORM 对象）"""
        from app.db.database import async_session_factory

        async with async_session_factory() as db:
            await db.execute(update(Note).where(Note.id == note_id).values(tags=tags))
            await db.commit()
        logger.info(f"自动标签写回完成: note_id={note_id}, tags={tags}")

    # ==================== AI 联机补全 ====================
    # 设计文档：docs/AI联机补全功能设计优化方案.md §3.6

    async def ai_autocomplete(
        self, content: str, cursor_position: int, title: str = ""
    ) -> dict:
        """
        AI 内联补全

        流程：
        1. 截取光标前 ≤ 800 + 光标后 ≤ 300 字符（N2：FIM 式 prefix/suffix）
        2. 加载 autocomplete prompt 并填充
        3. 调用轻量 LLM 生成补全
        4. 后处理：去除引号/多行解释，截断至 200 字符

        Args:
            content: 前端已截取的光标附近内容（前 1200 + 后 300 字符，I4）
            cursor_position: 光标位置（在截取内容中的偏移量）
            title: 笔记标题（提供上下文）

        Returns:
            {"completion": str, "model": str, "latency_ms": int}
            任何失败（未配置模型 / 模板缺失 / 调用异常）均返回空补全，不抛异常
        """
        role_model = model_for('autocomplete', self.autocomplete_model)
        from app.core.model_trace import set_trace_stage
        from app.utils.prompt_loader import format_prompt

        empty = {"completion": "", "model": "", "latency_ms": 0}

        if not role_model:
            return empty

        # 1. 截取上下文（N2：分别截取光标前后，供 FIM prompt 使用）
        # F9：上下文与补全长度上限改为可配（此前硬编码在代码里）
        max_before = int(os.getenv("AUTOCOMPLETE_MAX_CONTEXT", "800"))
        context_before, context_after = self._truncate_context(
            content, cursor_position, max_before=max_before
        )
        if len(context_before.strip()) < 10:
            return empty

        # 2. 构建 prompt（C7：模板文件缺失 / prompt.yaml 未配置时降级，而非 500）
        try:
            prompt = format_prompt(
                "autocomplete",
                title=title or "无标题",
                context_before=context_before,
                context_after=context_after or "（无）",
            )
        except (FileNotFoundError, KeyError) as e:
            logger.error(f"补全 prompt 模板加载失败: {e}")
            return empty

        # 3. 调用 LLM（失败静默降级，不打断用户输入）
        set_trace_stage("autocomplete")
        start = time.monotonic()
        try:
            response = await role_model.ainvoke([
                ("system", "你是一个笔记续写助手，直接输出续写内容。"),
                ("user", prompt),
            ])
            latency_ms = int((time.monotonic() - start) * 1000)
            raw_text = (
                response.content
                if isinstance(response.content, str)
                else str(response.content or "")
            )
        except Exception as e:
            logger.warning(f"AI 补全调用失败: {e}")
            from app.model_config.runtime import require_failure_policy
            require_failure_policy('autocomplete','disabled')
            return empty

        # 4. 后处理（F9：字符上限可配，默认 200）
        max_len = int(os.getenv("AUTOCOMPLETE_MAX_COMPLETION", "200"))
        completion = self._postprocess_completion(raw_text, max_len=max_len)
        model_name = getattr(role_model, "model_name", "") or ""

        return {"completion": completion, "model": model_name, "latency_ms": latency_ms}

    def _truncate_context(
        self, content: str, cursor_position: int,
        max_before: int = 800, max_after: int = 300,
    ) -> tuple:
        """
        截取光标前后上下文（N2：返回 (before, after) 用于 FIM prompt）

        Returns:
            (context_before, context_after)
            - context_before: 光标前 ≤ max_before 字符，优先段落边界截断
            - context_after:  光标后 ≤ max_after 字符
        """
        # 光标前：截取 + 段落边界优先
        before = content[:cursor_position]
        if len(before) > max_before:
            truncated = before[-max_before:]
            para_start = truncated.rfind("\n\n")
            if para_start > max_before // 2:  # 至少保留一半内容才用段落边界
                before = truncated[para_start + 2:]
            else:
                before = truncated

        # 光标后：直接截取 max_after 字符
        after = content[cursor_position:cursor_position + max_after]

        return before, after

    def _postprocess_completion(self, text: str, max_len: int = 200) -> str:
        """
        后处理模型输出：
        - 去除成对的首尾引号（模型常加 "" / '' / “”）
        - 只取第一行（避免模型输出多段解释）
        - 截断至 max_len（CJK 无空格时直接按字符截断）

        ⚠️ C3：引号集合必须用模块级 `_QUOTE_CHARS` 常量，禁止行内元组字面量
        """
        text = (text or "").strip()

        # 去除成对首尾引号（★ C3 修复 + F11：必须首尾成对匹配，
        # 避免 `"abc'` 这类单侧引号被误剥）
        if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
            text = text[1:-1].strip()

        # 只取第一行（避免模型输出多段解释）
        text = text.split("\n")[0].strip()

        # 截断（CJK 无空格，rsplit 找不到空格时返回原串，不会误截）
        if len(text) > max_len:
            head = text[:max_len]
            text = head.rsplit(" ", 1)[0] if " " in head else head

        return text

    # ==================== AI 写作辅助（续写 / 扩写 / 摘要） ====================

    async def ai_write_assist(
        self, content: str, mode: str = "continue", title: str = ""
    ) -> dict:
        """
        AI 写作辅助（用户主动触发，非高频）

        Args:
            content: 笔记内容（超长按 WRITE_ASSIST_MAX_CONTENT_CHARS 截断）
            mode: continue(续写) / expand(扩写) / summary(摘要)
            title: 笔记标题（提供上下文）

        Returns:
            {"result": str, "model": str, "latency_ms": int}
            未配置模型 / 内容过短 / 调用失败均返回空 result，不抛异常
        """
        role_model = model_for('write_assistant', self.chat_model)
        from app.core.model_trace import set_trace_stage
        from app.utils.prompt_loader import format_prompt

        empty = {"result": "", "model": "", "latency_ms": 0}

        if role_model is None:
            return empty

        text = (content or "").strip()
        if len(text) < 10:
            return empty

        mode_desc = _WRITE_MODES.get(mode, _WRITE_MODES["continue"])
        max_chars = int(os.getenv("WRITE_ASSIST_MAX_CONTENT_CHARS", "3000"))

        try:
            prompt = format_prompt(
                "write_assistant",
                mode_desc=mode_desc,
                title=title or "无标题",
                content=text[:max_chars],
            )
        except (FileNotFoundError, KeyError) as e:
            logger.error(f"写作辅助 prompt 加载失败: {e}")
            return empty

        set_trace_stage("write_assistant")
        start = time.monotonic()
        try:
            async with asyncio.timeout(float(os.getenv("WRITE_ASSIST_TIMEOUT", "60"))):
                response = await role_model.ainvoke([
                    ("system", "你是笔记写作助手，只输出结果正文，不要任何解释。"),
                    ("user", prompt),
                ])
            latency_ms = int((time.monotonic() - start) * 1000)
            raw = response.content if isinstance(response.content, str) else ""
        except Exception as e:
            logger.warning(f"写作辅助调用失败: {type(e).__name__}: {e}")
            from app.model_config.runtime import require_failure_policy
            require_failure_policy('write_assistant','disabled')
            return empty

        max_len = int(os.getenv("WRITE_ASSIST_MAX_COMPLETION", "1200"))
        result = self._postprocess_write_result(raw, max_len=max_len)
        model_name = getattr(role_model, "model_name", "") or ""

        return {"result": result, "model": model_name, "latency_ms": latency_ms}

    @staticmethod
    def _postprocess_write_result(text: str, max_len: int = 1200) -> str:
        """
        写作辅助输出后处理（与补全不同：**保留换行**，多段内容是正常输出）

        - 去除成对首尾引号
        - 截断至 max_len（保留段落结构，仅在超长时硬截）
        """
        text = (text or "").strip()
        if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
            text = text[1:-1].strip()
        if len(text) > max_len:
            text = text[:max_len].rstrip()
        return text

