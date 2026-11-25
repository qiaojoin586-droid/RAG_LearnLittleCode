"""
间隔重复回顾路由

端点：
- GET /review/today - 今日待回顾
- POST /review/{review_id}/complete - 标记回顾完成
- GET /review/stats - 回顾统计
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.success_response import success_response
from app.core.rate_limit import rate_limit
from app.core.model_trace import clear_trace_context, new_request_id, set_trace_context
from app.db.database import get_db
from app.utils.auth_utils import get_current_user_id
from app.services.review_service import ReviewService

router = APIRouter()

review_service = ReviewService()


@router.get("/review/today", summary="今日待回顾")
async def get_today_reviews(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取今日需要回顾的笔记列表"""
    try:
        reviews = await review_service.get_today_reviews(db, user_id)
        return success_response(data={"reviews": reviews, "count": len(reviews)})
    except Exception as e:
        logger.error(f"获取今日回顾失败: user_id={user_id}, error={e}")
        return success_response(data={"reviews": [], "count": 0})


@router.post("/review/{review_id}/complete", summary="标记回顾完成")
async def mark_reviewed(
    review_id: int,
    quality: int = 3,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    标记一条回顾记录为已完成
    
    根据艾宾浩斯曲线自动计算下次回顾时间。
    """
    review = await review_service.mark_reviewed(db, review_id, user_id, quality)
    return success_response(data={
        "review_id": review.id,
        "next_review_at": review.next_review_at.isoformat(),
        "interval_days": review.interval_days,
    })


@router.post("/review/{review_id}/questions", summary="AI 生成回顾问题")
async def generate_review_questions(
    review_id: int,
    user_id: str = Depends(get_current_user_id),      # ★ 必须排在 rate_limit 之前
    _rl=Depends(rate_limit(endpoint_limit=10)),       # ★ 参数顺序写法（C1）
    db: AsyncSession = Depends(get_db),
):
    """为回顾笔记生成 2-3 道复习题（LLM；同一笔记内容 6 小时内复用缓存）"""
    from main import init_manager

    # note 类端点无 trace 上下文（F17）→ 显式设置，保证费用按用户归属
    set_trace_context(new_request_id(), user_id=user_id, stage="review_question")
    try:
        data = await review_service.generate_questions(
            db, review_id, user_id, chat_model=init_manager.chat_model,
        )
    finally:
        clear_trace_context()
    return success_response(data=data)


@router.get("/review/stats", summary="回顾统计")
async def get_review_stats(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取用户的回顾统计信息"""
    try:
        stats = await review_service.get_review_stats(db, user_id)
        return success_response(data=stats)
    except Exception as e:
        logger.error(f"获取回顾统计失败: user_id={user_id}, error={e}")
        return success_response(data={"pending_today": 0, "total_reviews": 0})
