"""
聊天附件服务

管理 AI 对话中上传的图片/视频附件：
- 上传：校验（magic bytes/大小/配额）→ 落盘 data/chat_files/{user_id}/{file_id}.{ext} → 写 chat_attachments
- 访问：JWT 鉴权 + user_id 归属校验（防越权访问他人附件）
- 绑定：发送消息时回填 session_id，回复成功后回填 message_id
- 清理：孤儿附件（未绑定 + 超时）定时清理；会话删除时路由层级联清理
- 配额：按用户统计 chat_attachments.file_size 总和，与 USER_STORAGE_QUOTA_MB 对比

存储目录：data/chat_files/{user_id}/（不挂公开静态目录，预览走鉴权端点）
"""

import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, ErrorCode
from app.models.chat import ChatAttachment
from app.utils.file_handler import (
    validate_chat_attachment,
    get_safe_filename,
    ensure_dir,
    calculate_md5_bytes,
    CHAT_IMAGE_EXTENSIONS,
    CHAT_VIDEO_EXTENSIONS,
)

# 附件存储根目录（相对项目根）
CHAT_FILE_ROOT = os.getenv("CHAT_FILE_ROOT", os.path.join("data", "chat_files"))



class ChatAttachmentService:
    """聊天附件服务"""

    # ========== 存储路径 ==========

    @staticmethod
    def _user_dir(user_id: str) -> str:
        """用户附件目录（相对项目根）"""
        return os.path.join(CHAT_FILE_ROOT, user_id)

    @staticmethod
    def _attachment_path(user_id: str, file_id: str, ext: str) -> str:
        """附件存储路径（相对项目根）"""
        return os.path.join(CHAT_FILE_ROOT, user_id, f"{file_id}{ext}")

    # ========== 图片信息提取 ==========

    @staticmethod
    def get_image_dimensions(content: bytes) -> Optional[Tuple[int, int]]:
        """
        提取图片宽高（Pillow）

        失败（损坏图片等）返回 None，不影响上传流程。

        Args:
            content: 图片内容字节

        Returns:
            (width, height)，失败返回 None
        """
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(content)) as img:
                return img.width, img.height
        except Exception as e:
            logger.debug(f"图片尺寸提取失败: {e}")
            return None

    # ========== 上传 ==========

    async def save_upload(
        self, db: AsyncSession, user_id: str, filename: str, content: bytes
    ) -> ChatAttachment:
        """
        上传聊天附件（校验 → 配额 → 落盘 → 写表）

        附件处于孤儿状态（session_id/message_id 为空），超时未发送将被定时清理。

        Args:
            db: 数据库会话
            user_id: 用户 ID
            filename: 原始文件名
            content: 文件内容字节

        Returns:
            创建的 ChatAttachment（含 file_id）

        Raises:
            BusinessError: 类型/大小/配额校验失败
        """
        file_size = len(content)
        if file_size == 0:
            raise BusinessError(
                code=ErrorCode.INVALID_PARAMETER,
                detail="空文件无法上传",
            )

        # 按扩展名选择大小上限（图片 10MB / 视频 50MB）
        ext = Path(filename).suffix.lower()
        if ext in CHAT_IMAGE_EXTENSIONS:
            max_size_mb = int(os.getenv("CHAT_IMAGE_MAX_MB", "10"))
        elif ext in CHAT_VIDEO_EXTENSIONS:
            max_size_mb = int(os.getenv("CHAT_VIDEO_MAX_MB", "50"))
        else:
            max_size_mb = int(os.getenv("CHAT_IMAGE_MAX_MB", "10"))

        # 双重校验：扩展名白名单 + magic bytes（内容签名）
        info = validate_chat_attachment(filename, file_size, content, max_size_mb)

        # 用户配额校验（USER_STORAGE_QUOTA_MB，含已有附件）
        used = await self.get_user_storage_bytes(db, user_id)
        quota_mb = int(os.getenv("USER_STORAGE_QUOTA_MB", "500"))
        if used + file_size > quota_mb * 1024 * 1024:
            raise BusinessError(
                code=ErrorCode.STORAGE_QUOTA_EXCEEDED,
                detail=(
                    f"存储配额已满（已用 {used / 1024 / 1024:.1f}MB / "
                    f"{quota_mb}MB），请删除部分附件后重试"
                ),
            )

        file_id = uuid.uuid4().hex
        safe_name = get_safe_filename(filename)[:255]
        stored_path = self._attachment_path(user_id, file_id, info.ext)

        # 落盘
        ensure_dir(self._user_dir(user_id))
        with open(stored_path, "wb") as f:
            f.write(content)

        # 图片提取宽高（失败置 None）
        width = height = None
        if info.file_type == "image":
            dims = self.get_image_dimensions(content)
            if dims:
                width, height = dims

        attachment = ChatAttachment(
            file_id=file_id,
            user_id=user_id,
            file_type=info.file_type,
            mime_type=info.mime_type,
            original_name=safe_name,
            stored_path=stored_path,
            file_size=file_size,
            width=width,
            height=height,
            duration_sec=None,
            md5=calculate_md5_bytes(content),
            # 显式 UTC 赋值（与 chat_messages.created_at 的 utcnow() 一致）：
            # 孤儿清理按 UTC 判断，若用 MySQL NOW()（本地时区）会差 8 小时导致清理失效
            created_at=datetime.utcnow(),
        )
        db.add(attachment)
        await db.flush()
        logger.info(f"附件上传成功: file_id={file_id[:8]}, type={info.file_type}, size={file_size}")
        return attachment

    # ========== 查询 ==========

    async def get_owned(
        self, db: AsyncSession, user_id: str, file_id: str
    ) -> ChatAttachment:
        """
        获取附件（带归属校验）

        附件不存在或不属于当前用户时统一返回 404（防枚举探测）。

        Args:
            db: 数据库会话
            user_id: 用户 ID
            file_id: 附件 ID

        Returns:
            ChatAttachment

        Raises:
            BusinessError: 附件不存在或无权访问（404）
        """
        result = await db.execute(
            select(ChatAttachment).where(
                and_(
                    ChatAttachment.file_id == file_id,
                    ChatAttachment.user_id == user_id,
                )
            )
        )
        attachment = result.scalar_one_or_none()
        if not attachment:
            raise BusinessError(code=ErrorCode.ATTACHMENT_NOT_FOUND, http_status=404)
        return attachment

    async def get_owned_list(
        self, db: AsyncSession, user_id: str, file_ids: List[str]
    ) -> List[ChatAttachment]:
        """
        批量获取附件（仅返回归属当前用户的）

        用于发送消息时校验 attachment_ids 归属（不属于用户的直接忽略）。

        Args:
            db: 数据库会话
            user_id: 用户 ID
            file_ids: 附件 ID 列表

        Returns:
            归属当前用户的附件列表
        """
        if not file_ids:
            return []
        result = await db.execute(
            select(ChatAttachment).where(
                and_(
                    ChatAttachment.file_id.in_(file_ids),
                    ChatAttachment.user_id == user_id,
                )
            )
        )
        return list(result.scalars().all())

    async def get_user_storage_bytes(self, db: AsyncSession, user_id: str) -> int:
        """
        统计用户附件已用存储（字节）

        Args:
            db: 数据库会话
            user_id: 用户 ID

        Returns:
            已用字节数
        """
        from sqlalchemy import func
        result = await db.execute(
            select(func.coalesce(func.sum(ChatAttachment.file_size), 0)).where(
                ChatAttachment.user_id == user_id
            )
        )
        return int(result.scalar_one() or 0)

    # ========== 绑定 ==========

    async def bind_session(
        self, db: AsyncSession, user_id: str, file_ids: List[str], session_id: str
    ) -> None:
        """
        发送消息时回填 session_id（绑定会话，脱离孤儿状态）

        Args:
            db: 数据库会话
            user_id: 用户 ID
            file_ids: 附件 ID 列表
            session_id: 会话 ID
        """
        if not file_ids:
            return
        await db.execute(
            update(ChatAttachment)
            .where(
                and_(
                    ChatAttachment.file_id.in_(file_ids),
                    ChatAttachment.user_id == user_id,
                )
            )
            .values(session_id=session_id)
        )
        await db.flush()

    async def bind_message(
        self, db: AsyncSession, user_id: str, file_ids: List[str],
        session_id: str, message_id: int,
    ) -> None:
        """
        消息保存成功后回填 message_id（绑定消息，禁止单独删除）

        Args:
            db: 数据库会话
            user_id: 用户 ID
            file_ids: 附件 ID 列表
            session_id: 会话 ID
            message_id: 消息 ID
        """
        if not file_ids:
            return
        await db.execute(
            update(ChatAttachment)
            .where(
                and_(
                    ChatAttachment.file_id.in_(file_ids),
                    ChatAttachment.user_id == user_id,
                    ChatAttachment.session_id == session_id,
                )
            )
            .values(message_id=message_id)
        )
        await db.flush()

    # ========== 删除 ==========

    async def delete_unbound(
        self, db: AsyncSession, user_id: str, file_id: str
    ) -> bool:
        """
        删除附件（仅允许未绑定消息的孤儿/会话级附件）

        已绑定消息的附件不可单独删除（随会话级联清理）。

        Args:
            db: 数据库会话
            user_id: 用户 ID
            file_id: 附件 ID

        Returns:
            是否删除成功

        Raises:
            BusinessError: 附件不存在（404）或已绑定消息（400）
        """
        attachment = await self.get_owned(db, user_id, file_id)
        if attachment.message_id is not None:
            raise BusinessError(
                code=ErrorCode.INVALID_PARAMETER,
                detail="该附件已随消息发送，不可单独删除",
            )
        self._safe_remove(attachment.stored_path)
        await db.delete(attachment)
        await db.flush()
        logger.info(f"附件删除: file_id={file_id[:8]}, user={user_id[:8]}")
        return True

    async def cleanup_by_session(
        self, db: AsyncSession, session_id: str, user_id: Optional[str] = None
    ) -> int:
        """
        会话删除时级联清理附件（删文件 + 删行）

        由路由层调用（DELETE /chat/sessions/{session_id}），
        DatabaseSessionManager.delete_session 保持纯 DB 职责。

        Args:
            db: 数据库会话
            session_id: 会话 ID
            user_id: 可选，归属过滤（防越权清理他人附件）

        Returns:
            清理的附件数量
        """
        query = select(ChatAttachment).where(ChatAttachment.session_id == session_id)
        if user_id:
            query = query.where(ChatAttachment.user_id == user_id)
        result = await db.execute(query)
        attachments = list(result.scalars().all())

        for att in attachments:
            self._safe_remove(att.stored_path)
            await db.delete(att)
        await db.flush()
        if attachments:
            logger.info(f"会话级联清理附件: session={session_id[:12]}, count={len(attachments)}")
        return len(attachments)

    async def cleanup_session_tool_files(
        self, db: AsyncSession, session_id: str, user_id: str
    ) -> int:
        """
        会话删除时清理工具产出文件（PPT/TTS 物理文件，bug 修复）

        PPT/TTS 文件不落 chat_attachments 表，仅通过消息 attachments_json
        （file_type=ppt/tts + file_id）引用；删除会话时不清理会残留孤儿文件
        （TTS 无 TTL 清理机制，会永久占用磁盘）。

        由路由层调用（DELETE /chat/sessions/{session_id}），
        须在 DatabaseSessionManager.delete_session（删消息行）之前执行。

        Args:
            db: 数据库会话
            session_id: 会话 ID
            user_id: 用户 ID（目录归属）

        Returns:
            清理的文件数（pptx/json/mp3 按物理文件计）
        """
        from app.models.chat import ChatMessage
        from app.routers.tts_router import TTS_FILE_ROOT
        from app.services.ppt_service import PPT_FILE_ROOT

        query = select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.attachments_json.isnot(None),
        )
        result = await db.execute(query)
        messages = list(result.scalars().all())

        removed = 0
        for msg in messages:
            for att in msg.attachments_json or []:
                if not isinstance(att, dict):
                    continue
                ftype = att.get("file_type")
                fid = att.get("file_id")
                if ftype not in ("ppt", "tts") or not fid:
                    continue
                # 防御：file_id 必须为 uuid4().hex（32 位小写十六进制），
                # 防止脏数据/异常写入的路径穿越字符进入文件系统
                if not re.fullmatch(r"[0-9a-f]{32}", str(fid)):
                    logger.warning(f"跳过非法工具文件 file_id: {fid}")
                    continue
                if ftype == "ppt":
                    user_dir = Path(PPT_FILE_ROOT) / user_id
                    for suffix in (".pptx", ".json"):
                        target = user_dir / f"{fid}{suffix}"
                        if target.is_file():
                            try:
                                target.unlink()
                                removed += 1
                            except OSError as e:
                                logger.warning(f"PPT 文件清理失败: {target} - {e}")
                else:  # tts
                    target = Path(TTS_FILE_ROOT) / user_id / f"{fid}.mp3"
                    if target.is_file():
                        try:
                            target.unlink()
                            removed += 1
                        except OSError as e:
                            logger.warning(f"TTS 文件清理失败: {target} - {e}")
        if removed:
            logger.info(f"会话工具文件清理: session={session_id[:12]}, files={removed}")
        return removed

    async def cleanup_orphans(self, db: AsyncSession, ttl_hours: int = 24) -> int:
        """
        清理孤儿附件（未绑定会话 + 超过 TTL）

        由定时任务调用（scheduler，每日执行）。

        Args:
            db: 数据库会话
            ttl_hours: 孤儿存活时长（小时）

        Returns:
            清理的附件数量
        """
        deadline = datetime.utcnow() - timedelta(hours=ttl_hours)
        result = await db.execute(
            select(ChatAttachment).where(
                and_(
                    ChatAttachment.session_id.is_(None),
                    ChatAttachment.created_at < deadline,
                )
            )
        )
        attachments = list(result.scalars().all())

        for att in attachments:
            self._safe_remove(att.stored_path)
            await db.delete(att)
        await db.flush()
        if attachments:
            logger.info(f"孤儿附件清理: count={len(attachments)}, ttl={ttl_hours}h")
        return len(attachments)

    # ========== 文件工具 ==========

    @staticmethod
    def _safe_remove(relative_path: str) -> None:
        """
        安全删除文件（存在才删，失败仅告警）

        Args:
            relative_path: 相对项目根的存储路径
        """
        try:
            path = os.path.join(os.getcwd(), relative_path)
            if os.path.isfile(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"附件文件删除失败: {relative_path} - {e}")
