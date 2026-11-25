"""
定时任务调度器

使用 APScheduler 管理周期性后台任务：
- 每天凌晨 3:00 清理过期笔记（deleted_at 超过 14 天 → 物理删除 + 清理 ChromaDB 向量）

⚠️ reload 防护：uvicorn reload 模式会 fork 子进程，若父/子进程各启动一个 scheduler，
同一清理任务每天会执行两次。因此 init_scheduler() 仅由 main.py 在非 reload 模式下调用。
"""

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.logger_handler import logger

# 回收站自动清理阈值（天）——与 app/routers/note_router.py 的 RECYCLE_BIN_CLEANUP_DAYS 保持一致
CLEANUP_EXPIRED_DAYS = 14

scheduler = AsyncIOScheduler()


async def cleanup_expired_notes() -> int:
    """
    清理过期笔记（deleted_at 超过 14 天）→ 返回删除数量

    运行在请求上下文之外，需自行获取数据库会话和 NoteService 实例：
    - DB session：通过 async_session_factory 获取独立会话
    - NoteService：优先复用 init_manager.note_service（含 vector_store 与 chat_model），
      后台初始化未完成时降级为裸 NoteService（跳过向量清理，下次运行补清）
    """
    from app.db.database import async_session_factory
    from main import init_manager

    note_service = init_manager.note_service
    if note_service is None:
        from app.services.note_service import NoteService
        note_service = NoteService()

    async with async_session_factory() as db:
        try:
            count = await note_service.cleanup_expired_notes(db, days=CLEANUP_EXPIRED_DAYS)
            await db.commit()
            logger.info(f"定时清理过期笔记完成: 删除 {count} 条")
            return count
        except Exception as e:
            await db.rollback()
            logger.error(f"定时清理过期笔记失败: {e}")
            raise


async def cleanup_orphan_tts_files() -> int:
    """
    清理过期 TTS 音频文件（TTL 默认 24h，TTS_FILE_TTL_HOURS 控制）

    覆盖生成中断/未写入消息等无引用场景（生成时清理只能覆盖正常路径）。
    """
    from app.routers.tts_router import cleanup_expired_tts_files

    try:
        count = await cleanup_expired_tts_files()
        logger.info(f"定时清理过期 TTS 文件完成: 删除 {count} 个")
        return count
    except Exception as e:
        logger.error(f"定时清理过期 TTS 文件失败: {e}")
        raise


async def cleanup_orphan_chat_attachments() -> int:
    """
    清理孤儿聊天附件（未绑定会话 + 超过 TTL，默认 24h）

    删除文件 + chat_attachments 行，防止磁盘与配额失控。
    TTL 由环境变量 CHAT_ATTACHMENT_ORPHAN_TTL_HOURS 控制。
    """
    from app.db.database import async_session_factory
    from app.services.chat_attachment_service import ChatAttachmentService

    ttl_hours = int(os.getenv("CHAT_ATTACHMENT_ORPHAN_TTL_HOURS", "24"))
    async with async_session_factory() as db:
        try:
            count = await ChatAttachmentService().cleanup_orphans(db, ttl_hours=ttl_hours)
            await db.commit()
            logger.info(f"定时清理孤儿聊天附件完成: 删除 {count} 条")
            return count
        except Exception as e:
            await db.rollback()
            logger.error(f"定时清理孤儿聊天附件失败: {e}")
            raise


async def cleanup_expired_categories() -> int:
    """
    清理过期分类（deleted_at 超过 14 天）→ 返回删除数量

    与 cleanup_expired_notes 对齐；物理删除前保护活跃子分类（提升为顶级）。
    """
    from app.db.database import async_session_factory
    from app.services.category_service import CategoryService

    async with async_session_factory() as db:
        try:
            count = await CategoryService().cleanup_expired_categories(
                db, days=CLEANUP_EXPIRED_DAYS
            )
            await db.commit()
            logger.info(f"定时清理过期分类完成: 删除 {count} 个")
            return count
        except Exception as e:
            await db.rollback()
            logger.error(f"定时清理过期分类失败: {e}")
            raise


async def cleanup_expired_approvals() -> None:
    from app.ai_service.email_dispatch import maintain_email_dispatches
    from app.ai_service.tool_approvals import expire_approvals
    from app.db.database import async_session_factory

    await expire_approvals(async_session_factory)
    await maintain_email_dispatches(async_session_factory)


def init_scheduler() -> None:
    """初始化并启动定时任务（仅在非 reload 模式下由 main.py 调用）"""
    scheduler.add_job(
        cleanup_expired_notes,
        trigger=CronTrigger(hour=3, minute=0),
        id="cleanup_expired_notes",
        name="清理过期笔记",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        cleanup_orphan_chat_attachments,
        trigger=CronTrigger(hour=3, minute=20),
        id="cleanup_orphan_chat_attachments",
        name="清理孤儿聊天附件",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        cleanup_expired_categories,
        trigger=CronTrigger(hour=3, minute=30),
        id="cleanup_expired_categories",
        name="清理过期分类",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        cleanup_orphan_tts_files,
        trigger=CronTrigger(hour=3, minute=40),
        id="cleanup_orphan_tts_files",
        name="清理过期 TTS 文件",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(cleanup_expired_approvals, trigger=CronTrigger(minute='*/2'),
                      id='cleanup_expired_approvals', replace_existing=True, max_instances=1)
    scheduler.start()
    logger.info(
        "定时任务调度器已启动（03:00 清理回收站过期笔记，"
        "03:20 清理孤儿聊天附件，03:30 清理过期分类，03:40 清理过期 TTS 文件）"
    )


def shutdown_scheduler() -> None:
    """关闭定时任务调度器（wait=False 避免阻塞应用关闭流程）"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("定时任务调度器已关闭")
