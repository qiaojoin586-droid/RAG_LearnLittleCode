"""
间隔重复回顾服务

基于艾宾浩斯遗忘曲线算法实现间隔重复回顾：
- 回顾间隔：1 / 2 / 4 / 7 / 15 / 30 天
- 今日待回顾查询
- 标记回顾完成（更新下次回顾时间）
- 生成回顾问题（LLM）
"""
from app.model_config.runtime import model_for, template_for
from app.model_config.snapshots import current_snapshot

from datetime import datetime, timedelta
from typing import List, Optional

import asyncio
import json
import os
import re
import time

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.models.review import ReviewRecord
from app.models.note import Note

# 艾宾浩斯遗忘曲线间隔（天）
EBBINGHAUS_INTERVALS = [1, 2, 4, 7, 15, 30]

# 复习题缓存 TTL（秒）：同一笔记内容未变时复用，避免重复计费
REVIEW_QUESTION_CACHE_TTL = 6 * 3600


class ReviewService:
    """
    间隔重复回顾服务
    
    实现艾宾浩斯遗忘曲线的间隔重复算法。
    每次回顾后根据当前间隔索引递增到下一个间隔。
    """
    
    async def get_today_reviews(
        self, db: AsyncSession, user_id: str
    ) -> List[dict]:
        """
        获取今日待回顾的笔记
        
        查询 next_review_at <= 当前时间 的回顾记录，
        关联查询笔记内容，返回完整信息。
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            
        Returns:
            待回顾笔记列表（包含笔记内容和回顾信息）
        """
        now = datetime.utcnow()
        
        result = await db.execute(
            select(ReviewRecord, Note)
            .join(Note, ReviewRecord.note_id == Note.id)
            .where(
                and_(
                    ReviewRecord.user_id == user_id,
                    ReviewRecord.next_review_at <= now,
                    Note.deleted_at.is_(None),
                )
            )
            .order_by(ReviewRecord.next_review_at.asc())
        )
        
        reviews = result.all()
        
        return [
            {
                "review_id": review.id,
                "note_id": review.note_id,
                "note_title": note.title,
                "note_content": note.content,
                "review_count": review.review_count,
                "interval_days": review.interval_days,
                "next_review_at": review.next_review_at.isoformat(),
            }
            for review, note in reviews
        ]
    
    async def mark_reviewed(
        self, db: AsyncSession, review_id: int, user_id: str, quality: int = 3
    ) -> ReviewRecord:
        """
        标记回顾完成，更新下次回顾时间
        
        根据艾宾浩斯曲线递增间隔：
        - 当前间隔索引 + 1 → 新的间隔天数
        - 如果已到最大间隔，保持最后一个间隔
        
        Args:
            db: 数据库会话
            review_id: 回顾记录 ID
            user_id: 用户 ID
            quality: 回顾质量评分（0-5，预留 SM-2 算法）
            
        Returns:
            更新后的回顾记录
        """
        # 查询回顾记录
        result = await db.execute(
            select(ReviewRecord).where(
                and_(ReviewRecord.id == review_id, ReviewRecord.user_id == user_id)
            )
        )
        review = result.scalar_one_or_none()
        
        if not review:
            from app.core.failed_response import BusinessError, ErrorCode
            raise BusinessError(code=ErrorCode.NOTE_NOT_FOUND, message="回顾记录不存在", http_status=404)
        
        # 更新回顾信息
        review.review_count += 1
        review.quality = quality
        review.reviewed_at = datetime.utcnow()
        
        # 计算下次回顾时间（艾宾浩斯间隔递增）
        current_index = EBBINGHAUS_INTERVALS.index(review.interval_days) if review.interval_days in EBBINGHAUS_INTERVALS else -1
        next_index = min(current_index + 1, len(EBBINGHAUS_INTERVALS) - 1)
        review.interval_days = EBBINGHAUS_INTERVALS[next_index]
        
        review.next_review_at = datetime.utcnow() + timedelta(days=review.interval_days)
        
        await db.flush()
        
        logger.info(
            f"回顾完成: review_id={review_id}, "
            f"interval={review.interval_days}天, "
            f"next_review={review.next_review_at}"
        )
        
        return review
    
    async def get_review_stats(self, db: AsyncSession, user_id: str) -> dict:
        """
        获取用户的回顾统计信息
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            
        Returns:
            统计信息字典（今日待回顾数、总回顾次数、今日已完成、连续天数）
        """
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 今日待回顾数
        pending_result = await db.execute(
            select(func.count()).select_from(ReviewRecord).where(
                and_(
                    ReviewRecord.user_id == user_id,
                    ReviewRecord.next_review_at <= now,
                )
            )
        )
        pending_count = pending_result.scalar()
        
        # 总回顾次数
        total_result = await db.execute(
            select(func.sum(ReviewRecord.review_count)).where(
                ReviewRecord.user_id == user_id
            )
        )
        total_reviews = total_result.scalar() or 0
        
        # 今日已完成数（reviewed_at 在今天之后的记录数）
        completed_result = await db.execute(
            select(func.count()).select_from(ReviewRecord).where(
                and_(
                    ReviewRecord.user_id == user_id,
                    ReviewRecord.reviewed_at >= today_start,
                )
            )
        )
        completed_today = completed_result.scalar() or 0
        
        # 连续天数：从今天往前数，每天有 reviewed_at 记录则累加
        streak_days = 0
        check_date = today_start
        while True:
            day_end = check_date + timedelta(days=1)
            streak_result = await db.execute(
                select(func.count()).select_from(ReviewRecord).where(
                    and_(
                        ReviewRecord.user_id == user_id,
                        ReviewRecord.reviewed_at >= check_date,
                        ReviewRecord.reviewed_at < day_end,
                    )
                )
            )
            if (streak_result.scalar() or 0) > 0:
                streak_days += 1
                check_date -= timedelta(days=1)
            else:
                break
        
        return {
            "pending_today": pending_count,
            "total_reviews": total_reviews,
            "completed_today": completed_today,
            "streak_days": streak_days,
        }

    async def generate_questions(
        self, db: AsyncSession, review_id: int, user_id: str, chat_model=None
    ) -> dict:
        """
        为某条回顾记录对应的笔记生成复习题（LLM）

        流程：校验归属 → 查缓存（6h，key 含 note.updated_at 以随笔记变更失效）
              → 调 LLM → 解析 JSON → 写缓存

        Args:
            db: 数据库会话
            review_id: 回顾记录 ID
            user_id: 当前用户（归属校验）
            chat_model: LLM 实例（由路由层从 init_manager 注入，便于测试替换）

        Returns:
            {"questions": [{"question": str, "answer": str}], "model": str,
             "latency_ms": int, "cached": bool}
            任何失败（无模型 / 内容过短 / 调用或解析异常）返回空 questions，不抛异常
        """
        chat_model = model_for('review_question', chat_model)
        from app.core.failed_response import BusinessError, ErrorCode

        empty = {"questions": [], "model": "", "latency_ms": 0, "cached": False}

        result = await db.execute(
            select(ReviewRecord, Note)
            .join(Note, ReviewRecord.note_id == Note.id)
            .where(
                and_(
                    ReviewRecord.id == review_id,
                    ReviewRecord.user_id == user_id,
                    Note.deleted_at.is_(None),
                )
            )
        )
        row = result.first()
        if not row:
            raise BusinessError(
                code=ErrorCode.NOTE_NOT_FOUND, message="回顾记录不存在", http_status=404
            )
        _review, note = row

        if chat_model is None:
            return empty
        return await self._questions_from_note(note, chat_model)

    async def _questions_from_note(self, note, chat_model) -> dict:
        """
        出题主体（LLM + 缓存），与 DB 查询解耦以便单测

        Args:
            note: 具备 id / title / content / updated_at 的对象（ORM 或替身）
            chat_model: LLM 实例
        """
        chat_model = model_for('review_question', chat_model)
        empty = {"questions": [], "model": "", "latency_ms": 0, "cached": False}

        content = (note.content or "").strip()
        if len(content) < 50:
            return empty

        # 缓存键包含 updated_at：笔记内容变化后自动失效
        cache_key = f"review_q:{note.id}:{int(note.updated_at.timestamp())}"
        if current_snapshot() is not None:
            cache_key += ':'+current_snapshot().release_id
        redis = None
        try:
            from app.db.redis_client import get_redis

            redis = get_redis()
            cached = await redis.get(cache_key)
            if cached:
                data = json.loads(cached)
                data["cached"] = True
                return data
        except Exception as e:  # 缓存故障不影响生成
            logger.debug(f"复习题缓存读取跳过: {type(e).__name__}: {e}")
            redis = None

        from app.core.model_trace import set_trace_stage
        from app.utils.prompt_loader import format_prompt

        max_chars = int(os.getenv("REVIEW_QUESTION_MAX_CONTENT_CHARS", "2000"))
        try:
            prompt = format_prompt(
                "review_question",
                title=note.title or "无标题",
                content=content[:max_chars],
            )
        except (FileNotFoundError, KeyError) as e:
            logger.warning(f"复习题 prompt 加载失败: {e}")
            return empty

        set_trace_stage("review_question")
        start = time.monotonic()
        try:
            async with asyncio.timeout(30):
                response = await chat_model.ainvoke([
                    ("system", "你是复习助手，只输出 JSON，不要任何解释。"),
                    ("user", prompt),
                ])
            latency_ms = int((time.monotonic() - start) * 1000)
            raw = response.content if isinstance(response.content, str) else ""
        except Exception as e:
            logger.warning(f"复习题生成失败: note_id={note.id}, {type(e).__name__}: {e}")
            from app.model_config.runtime import require_failure_policy
            require_failure_policy('review_question','disabled')
            return empty

        questions = self._parse_questions(raw)
        payload = {
            "questions": questions,
            "model": getattr(chat_model, "model_name", "") or "",
            "latency_ms": latency_ms,
            "cached": False,
        }

        if questions and redis is not None:
            try:
                await redis.set(cache_key, json.dumps(payload, ensure_ascii=False),
                                ex=REVIEW_QUESTION_CACHE_TTL)
            except Exception as e:
                logger.debug(f"复习题缓存写入跳过: {type(e).__name__}: {e}")

        return payload

    @staticmethod
    def _parse_questions(raw: str) -> list:
        """
        解析 LLM 返回的复习题 JSON（纯函数，容错优先）

        兼容 `{"questions": [...]}` 与裸数组两种形态；过滤无问题的条目，最多 3 道。
        """
        text = re.sub(r"```(?:json)?\s*", "", raw or "").replace("```", "").strip()
        if not text:
            return []

        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"[\[{].*[\]}]", text, re.S)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return []

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("questions")
        else:
            items = None
        if not isinstance(items, list):
            return []

        questions: list = []
        for item in items:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not question:
                continue
            questions.append({"question": question, "answer": answer})
        return questions[:3]
