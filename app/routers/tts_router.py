"""
TTS 音频下载路由（外部 API 工具接入文档 §4.3/§5.2）

GET /api/v1/tts/{file_id} —— JWT 鉴权 + 归属校验 + FileResponse 下载。

安全要点（与 ppt_router 完全一致）：
- file_id 必须匹配 ^[0-9a-f]{32}$（防路径穿越）
- 按 {user_id} 目录隔离（越权/不存在 → 404）
- 不走公开静态目录（音频含笔记朗读内容，参照 PPT 的安全模型）
"""
import os
import re
import time
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.logger_handler import logger
from app.utils.auth_utils import get_current_user_id

router = APIRouter()

# TTS 音频根目录（环境变量可覆盖，参照 PPT_FILE_ROOT 模式）
TTS_FILE_ROOT = os.getenv("TTS_FILE_ROOT", os.path.join("data", "tts"))

# TTS 文件 TTL（小时，默认 24h，与 PPT file_ttl_hours 语义一致）
TTS_FILE_TTL_HOURS = int(os.getenv("TTS_FILE_TTL_HOURS", "24"))
# 每用户 TTS 文件数配额（防磁盘膨胀；PPT 为 20，TTS 单文件更小，放宽到 50）
TTS_MAX_FILES_PER_USER = int(os.getenv("TTS_MAX_FILES_PER_USER", "50"))

# file_id = uuid4().hex（32 位小写十六进制）
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

TTS_MEDIA_TYPE = "audio/mpeg"


@router.get("/tts/{file_id}", summary="下载 TTS 生成的音频文件")
async def download_tts(
    file_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    下载 TTS 音频文件（JWT 鉴权 + 归属校验）

    Args:
        file_id: 生成文件 ID（uuid4().hex）
        user_id: 当前用户 ID（参数级 Depends，仅一次 JWT 解码，见 ppt_router §6.2）

    Returns:
        FileResponse（.mp3）
    """
    # ① file_id 格式校验，防路径穿越
    if not _FILE_ID_RE.match(file_id):
        raise HTTPException(status_code=400, detail="无效的文件 ID")

    # ② 归属校验：{user_id}/{file_id}.mp3 必须存在
    audio_path = Path(TTS_FILE_ROOT) / user_id / f"{file_id}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")

    # ③ FileResponse 下载
    return FileResponse(
        audio_path,
        filename=f"{file_id}.mp3",
        media_type=TTS_MEDIA_TYPE,
    )


def cleanup_user_tts_files(user_id: str) -> int:
    """
    生成时按用户清理过期/超配额 TTS 文件（与 PPT._cleanup 对称，防磁盘膨胀）

    - TTL 过期（默认 24h，TTS_FILE_TTL_HOURS）：文件生成后仅下载不再写入，
      mtime 即生成时间；TTL 到期后下载端点返回 404
    - 配额超限：保留最新 TTS_MAX_FILES_PER_USER 个

    由 text_to_speech 工具在生成后调用（同步函数，放线程池外执行）。

    Args:
        user_id: 用户 ID

    Returns:
        清理的文件数
    """
    user_dir = Path(TTS_FILE_ROOT) / user_id
    if not user_dir.is_dir():
        return 0

    now = time.time()
    deadline = now - TTS_FILE_TTL_HOURS * 3600

    mp3_files: List[Path] = sorted(
        (p for p in user_dir.glob("*.mp3") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # 新的在前
    )

    removed = 0
    for path in user_dir.glob('*.mp3.part'):
        try:
            if path.stat().st_mtime < deadline:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            logger.warning('TTS 临时文件清理失败')
    # ① TTL 过期 → 删除
    alive = []
    for p in mp3_files:
        try:
            if p.stat().st_mtime < deadline:
                p.unlink()
                removed += 1
            else:
                alive.append(p)
        except OSError as e:
            logger.warning(f"TTS TTL 清理失败: {p} - {e}")
            alive.append(p)

    # ② 配额超限 → 删除最旧的
    for p in alive[TTS_MAX_FILES_PER_USER:]:
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"TTS 配额清理失败: {p} - {e}")

    if removed:
        logger.info(f"TTS 用户目录清理: user={user_id[:8]}, removed={removed}")
    return removed


async def cleanup_expired_tts_files() -> int:
    """
    清理全部用户的过期 TTS 文件（定时任务兜底）

    覆盖生成中断/未写入消息等无引用场景（生成时清理只能覆盖正常路径）。
    由 scheduler 每日调用；PPT 因生成时 _cleanup 已含 TTL+配额，
    暂无全局定时任务（后续可对称添加）。
    """
    root = Path(TTS_FILE_ROOT)
    if not root.is_dir():
        return 0

    removed = 0
    for user_dir in root.iterdir():
        if user_dir.is_dir():
            removed += cleanup_user_tts_files(user_dir.name)
    if removed:
        logger.info(f"TTS 全局过期清理完成: removed={removed}")
    return removed
