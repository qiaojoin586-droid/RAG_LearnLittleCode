"""
Agent 工具集

定义 Agent 可使用的 12 个异步工具：
1. what_time_is_now - 获取当前时间
2. get_user_info_tools - 获取当前用户基本信息（用户名、邮箱、ID）
3. search_notes_tool - 语义搜索笔记（返回 200 字符摘要）
4. get_note_content_tool - 获取单篇笔记完整内容（邮件发送/导出用）
5. get_note_stats_tool - 笔记分类统计
6. get_today_reviews_tool - 获取今日待回顾笔记
7. mark_reviewed_tool - 标记回顾完成
8. create_note_tool - 创建新笔记
9. update_note_tool - 更新已有笔记
10. get_related_notes_tool - 获取关联推荐
11. send_email - 发送邮件（笔记转 Markdown/PDF 附件发送到用户邮箱）
12. generate_ppt_tool - 根据选中的笔记生成讲解 PPT（.pptx，设计方案 §4.1）
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from langchain_core.tools import tool
from sqlalchemy import select

from app.ai_service.tool_operations import request_email_approval, run_tool_write
from app.ai_service.email_dispatch import email_arguments
from app.ai_service.note_tool_writes import apply_write, replay_write
from app.ai_service.detached_chat import restrict_background_tools
from app.ai_service.tool_registry import init_tool_registry, tool_registry
from app.core.logger_handler import logger
from app.core.failed_response import BusinessError
from app.utils.factory import get_shared_async_client


# ============================================================
# 外部 API 工具（直接集成，外部 API 工具接入文档 §4.1/§4.2）
# 模块级 @tool：无用户上下文依赖，翻译/计算结果直接返回
# ============================================================

_DEEPL_MAX_CHARS = 5000


@tool
async def translate_text(text: str, target_lang: str, source_lang: str = "") -> str:
    """使用 DeepL 将文本翻译为目标语言（质量优于 LLM 原生翻译，术语一致）。
    适用场景：用户要求「翻译 / 翻译成 XX 语言」，尤其是长文、论文摘要、术语密集文本。

    Args:
        text: 要翻译的文本（最多 5000 字符/次，超出自动截断并提示）
        target_lang: 目标语言代码（如 ZH, EN, JA, KO, DE, FR）
        source_lang: 源语言代码（可选，留空自动检测）
    """
    api_key = os.getenv("DEEPL_API_KEY")
    if not api_key:
        return "翻译服务未配置（缺少 DEEPL_API_KEY）"
    # Free 与 Pro 端点不同，通过环境变量区分（默认 Free）
    base_url = os.getenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
    truncated = len(text) > _DEEPL_MAX_CHARS
    if truncated:
        text = text[:_DEEPL_MAX_CHARS]
    params = {
        "text": text,
        "target_lang": target_lang.upper(),
    }
    if source_lang:
        params["source_lang"] = source_lang.upper()
    try:
        # 复用共享 httpx 客户端（TCP/TLS 连接复用），
        # timeout=15 为请求级超时，覆盖共享客户端默认 120s
        client = get_shared_async_client()
        resp = await client.post(
            base_url, data=params,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        logger.error(f"DeepL 翻译失败: {e}")
        return f"翻译失败：{e}。请稍后重试。"
    translations = result.get("translations", [])
    if not translations:
        return "翻译结果为空"
    output = translations[0]["text"]
    if truncated:
        output += f"\n\n[提示：原文超过 {_DEEPL_MAX_CHARS} 字符，已截断]"
    return output


@tool
async def wolfram_calculate(query: str) -> str:
    """使用 Wolfram Alpha 进行精确的数学计算、单位换算和科学查询。
    适用于：解方程、求导、积分、单位换算、科学数据查询等。
    当 LLM 自身计算不确定时，优先调用本工具获取可靠结果。

    Args:
        query: 计算查询（英文效果最佳，如 "solve x^2+5x+6=0"、"100 km/h in m/s"）
    """
    app_id = os.getenv("WOLFRAM_APP_ID")
    if not app_id:
        return "计算服务未配置（缺少 WOLFRAM_APP_ID）"
    try:
        # 复用共享 httpx 客户端（连接复用），timeout=15 为请求级超时
        client = get_shared_async_client()
        resp = await client.get(
            "https://api.wolframalpha.com/v1/result",
            params={"appid": app_id, "i": query},
            timeout=15,
        )
        if resp.status_code == 400:
            return f"Wolfram Alpha 无法理解查询: {query}"
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"Wolfram Alpha 计算失败: {e}")
        return f"计算失败：{e}。请稍后重试。"


def create_agent_tools(
    user_id: str,
    note_service=None,
    review_service=None,
    db_session_factory=None,
    email_service=None,
    ppt_service=None,
    groups: Optional[List[str]] = None,
):
    """
    创建 Agent 工具集工厂函数

    每次调用创建新的工具实例，绑定当前用户上下文。
    支持按需加载：通过 groups 参数指定要加载的工具组，
    未指定时返回全部工具（向后兼容）。

    Args:
        user_id: 当前用户 ID
        note_service: 笔记服务实例
        review_service: 回顾服务实例
        db_session_factory: 数据库会话工厂
        email_service: 邮件发送服务实例（send_email 工具使用）
        ppt_service: PPT 生成服务实例（generate_ppt_tool 工具使用，§6.4）
        groups: 要加载的工具组名称列表（如 ["base", "note_read"]）。
                None 表示加载全部工具。

    Returns:
        工具列表
    """

    # 用户信息延迟缓存：首次调用 get_user_info_tools 时查询数据库，
    # 后续调用命中缓存。始终初始化，避免 db_session_factory 为 None 时 nonlocal 引用报错
    _user_cache = {"fetched": False, "user": None}

    @tool
    async def what_time_is_now() -> str:
        """获取当前日期和时间，返回格式为 YYYY-MM-DD HH:MM:SS"""
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")

    @tool
    async def get_user_info_tools() -> str:
        """获取当前登录用户的基本信息（用户名、邮箱、用户ID）"""
        nonlocal _user_cache
        if not _user_cache["fetched"]:
            if db_session_factory:
                from sqlalchemy import select
                from app.models.user import User

                async with db_session_factory() as db:
                    result = await db.execute(select(User).where(User.uuid == user_id))
                    _user_cache["user"] = result.scalar_one_or_none()
            _user_cache["fetched"] = True

        user = _user_cache["user"]
        if user:
            email_info = user.email if user.email else "未设置邮箱"
            return f"用户名: {user.username}\n邮箱: {email_info}\n用户ID: {user_id}"
        return f"当前用户 ID: {user_id}（用户信息获取失败）"

    @tool
    async def search_notes_tool(query: str, top_k: int = 5) -> str:
        """
        语义搜索用户的笔记

        注意：返回的笔记内容仅为摘要（前 200 字符）。若需要单篇笔记的完整内容
        （如导出、发送邮件），请先搜索获取 note_id，再调用 get_note_content_tool。

        Args:
            query: 搜索关键词
            top_k: 返回结果数量，默认 5
        """
        if not db_session_factory or not note_service:
            return "笔记服务未初始化"

        async with db_session_factory() as db:
            results = await note_service.semantic_search(db, user_id, query, top_k)

            if not results:
                return "未找到相关笔记"

            output = []
            for note, score in results:
                output.append(f"[{score:.2f}] ID:{note.id} | {note.title}\n{note.content[:200]}...")

            return "\n\n---\n\n".join(output)

    @tool
    async def get_note_content_tool(note_id: str) -> str:
        """
        获取单篇笔记的完整内容（Markdown，含标题、分类/标签元信息和全部正文）。

        发送邮件、导出等需要完整内容的场景，必须先用 search_notes_tool 搜索
        目标笔记获取 note_id，再调用本工具获取完整内容。
        注意：search_notes_tool 只返回 200 字符摘要，不得直接用作邮件正文。

        Args:
            note_id: 笔记 ID（可通过 search_notes_tool 搜索获取）
        """
        if not db_session_factory or not note_service:
            return "服务未初始化"

        # 完整内容上限（超出部分截断并明确提示，保护上下文窗口）
        MAX_CONTENT_CHARS = 50000

        async with db_session_factory() as db:
            try:
                note = await note_service.get_note(db, note_id, user_id)
            except Exception as e:
                # 审查 M15：内部异常细节仅入日志，工具返回类别级提示
                logger.warning(f"读取笔记失败: note_id={note_id}, error={e}")
                return "未找到笔记或无权访问"

        content = note.content or ""
        truncated = len(content) > MAX_CONTENT_CHARS
        if truncated:
            content = content[:MAX_CONTENT_CHARS]

        tags_str = ",".join(note.tags or []) or "无"
        updated_str = note.updated_at.strftime("%Y-%m-%d") if note.updated_at else "未知"
        header = (
            f"# {note.title}\n\n"
            f"> 分类：{note.category or '未分类'} | 标签：{tags_str} | 更新日期：{updated_str}\n\n"
            f"---\n\n"
        )
        result_str = header + content
        if truncated:
            result_str += f"\n\n[提示：笔记内容超过 {MAX_CONTENT_CHARS} 字符，已截断]"
        return result_str

    @tool
    async def get_note_stats_tool() -> str:
        """获取笔记分类统计信息（各分类的笔记数量）"""
        if not db_session_factory:
            return "数据库未初始化"

        from sqlalchemy import and_, func, select
        from app.models.note import Note
        from app.models.category import NoteCategory

        async with db_session_factory() as db:
            # 单次 LEFT JOIN 合并原两次查询（分类统计 + 未分类统计），语义与原实现一致：
            # - category_id IS NULL → 归入"未分类"（COALESCE 兜底）
            # - 分类已删但笔记未删 → 不计入任何统计（原实现内连接排除此类笔记，
            #   未分类统计也只查 IS NULL；此处用 WHERE 显式排除保持同语义）
            category_name = func.coalesce(NoteCategory.name, "未分类").label("category_name")
            result = await db.execute(
                select(category_name, func.count().label("note_count"))
                .select_from(Note)
                .outerjoin(
                    NoteCategory,
                    and_(
                        Note.category_id == NoteCategory.id,
                        NoteCategory.deleted_at.is_(None),
                    ),
                )
                .where(
                    Note.user_id == user_id,
                    Note.deleted_at.is_(None),
                    (Note.category_id.is_(None)) | (NoteCategory.id.is_not(None)),
                )
                .group_by(NoteCategory.id, NoteCategory.name)
            )
            stats = list(result.all())

            if not stats:
                return "暂无笔记"

            # 未分类行固定排在最后（与原实现输出顺序一致）
            rows = sorted(stats, key=lambda r: r[0] == "未分类")
            lines = [f"- {name}: {count} 篇" for name, count in rows]
            return "笔记分类统计:\n" + "\n".join(lines)

    @tool
    async def get_today_reviews_tool() -> str:
        """获取今日待回顾的笔记列表"""
        if not db_session_factory or not review_service:
            return "服务未初始化"

        async with db_session_factory() as db:
            reviews = await review_service.get_today_reviews(db, user_id)

            if not reviews:
                return "今日没有待回顾的笔记"

            lines = []
            for r in reviews:
                lines.append(f"- [{r['note_title']}] (第{r['review_count']+1}次回顾)")

            return f"今日待回顾 ({len(reviews)} 篇):\n" + "\n".join(lines)

    async def _note_write(name, arguments):
        async def write(db, after_commit):
            return await apply_write(db, user_id, name, arguments, after_commit, note_service, review_service)

        async def replay(db, reference):
            return await replay_write(db, user_id, name, reference, note_service)

        return await run_tool_write(db_session_factory, user_id, name, arguments, write, replay)

    @tool(response_format="content_and_artifact")
    async def mark_reviewed_tool(review_id: int) -> tuple[str, dict | None]:
        """
        标记一条回顾记录为已完成

        Args:
            review_id: 回顾记录 ID
        """
        if not db_session_factory or not review_service:
            return "服务未初始化", None

        return await _note_write('mark_reviewed_tool', {'review_id': review_id})

    @tool(response_format="content_and_artifact")
    async def create_note_tool(title: str, content: str, tags: str = "", category: str = "") -> tuple[str, dict | None]:
        """
        创建一条新笔记

        Args:
            title: 笔记标题
            content: 笔记内容（支持 Markdown）
            tags: 标签，逗号分隔（如 "Python,FastAPI"）
            category: 分类名称
        """
        if not db_session_factory or not note_service:
            return "服务未初始化", None

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        return await _note_write('create_note_tool', {
            'title': title, 'content': content, 'tags': tag_list, 'category': category or None,
        })

    @tool(response_format="content_and_artifact")
    async def update_note_tool(note_id: str, title: str = None, content: str = None, tags: str = None,
                               category: str = None) -> tuple[str, dict | None]:
        """
        更新一条已有笔记的内容、标题、标签或分类。
        只需传入要修改的字段，未传入的字段保持不变。
        使用前请先通过 search_notes_tool 搜索目标笔记获取其 ID。

        Args:
            note_id: 笔记 ID（必填，可通过 search_notes_tool 搜索获取）
            title: 新标题（可选，不传则保持原标题不变）
            content: 新内容（可选，支持 Markdown，不传则保持原内容不变。注意：这是完整替换，如需追加内容请先读取原内容再拼接）
            tags: 新标签（可选，逗号分隔如 "Python,FastAPI"，不传则保持原标签不变）
            category: 新分类名称（可选，不传则保持原分类不变）
        """
        if not db_session_factory or not note_service:
            return "服务未初始化", None

        update_data = {}
        if title is not None:
            update_data["title"] = title
        if content is not None:
            update_data["content"] = content
        if tags is not None:
            update_data["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

        if not update_data and category is None:
            return "未提供任何要更新的字段", None

        try:
            return await _note_write('update_note_tool', {'note_id': note_id, **update_data, 'category': category})
        except Exception as error:
            logger.warning(f"笔记更新失败: note_id={note_id}, error_type={type(error).__name__}")
            return "笔记更新失败，请稍后重试", None

    @tool
    async def get_related_notes_tool(note_title: str, top_k: int = 3) -> str:
        """
        获取与指定标题相关的笔记推荐

        Args:
            note_title: 参考笔记标题
            top_k: 返回数量，默认 3
        """
        if not db_session_factory or not note_service:
            return "服务未初始化"

        async with db_session_factory() as db:
            results = await note_service.semantic_search(db, user_id, note_title, top_k)

            if not results:
                return "未找到相关笔记"

            output = []
            for note, score in results:
                output.append(f"[{score:.2f}] {note.title}")

            return "相关笔记:\n" + "\n".join(output)

    @tool(response_format='content_and_artifact')
    async def send_email(to: str, subject: str, body: str, format: str = "md") -> tuple[str, dict | None]:
        """
        申请发送邮件。助手提交收件人、主题、正文和格式，用户在待确认操作中核对并决定。
        等待确认时请提示用户查看申请。只有回执表明邮件受理或收件后，才报告对应状态。

        Args:
            to: 收件人邮箱地址（用户明确给出的目标地址，或用户本人绑定邮箱）
            subject: 邮件主题（如 "笔记导出：{笔记标题}"）
            body: 邮件正文（Markdown 格式，建议包含笔记标题、分类/标签元信息和完整内容）
            format: 附件格式："md"（Markdown 附件，默认）/ "pdf"（PDF 附件）/ "text"（仅正文，无附件）
        """
        if not email_service or not email_service.available:
            return '邮件服务未初始化，请联系管理员', None
        try:
            arguments = email_arguments(to, subject, body, format)
            return await request_email_approval(db_session_factory, user_id, arguments)
        except BusinessError as error:
            return f'邮件申请失败：{error.message}', None

    @tool
    async def generate_ppt_tool(
        note_ids: str,          # 必填：笔记 ID，逗号分隔（如 "id1,id2,id3"），须来自用户引用的笔记
        title: str = "",        # 可选：PPT 主题，默认由 LLM 根据笔记内容拟定
        style: str = "business",  # 可选：风格预设 business / academic / minimal
        template_id: str = "",  # 可选：用户选择的 PPT 模板 ID（<ppt_template> 内提供的），为空时用默认版式
    ) -> str:
        """
        根据用户选中的一篇或多篇笔记，生成一份讲解用 PPT（.pptx 文件）。
        适用场景：用户说「把这几篇笔记做成PPT / 生成讲解幻灯片 / 整理成演示文稿」。
        注意：note_ids 必须是用户引用笔记中的 ID（<referenced_notes> 内提供的），
        逗号分隔，可一次传入多篇，每篇笔记会生成独立的章节。
        template_id 须来自用户消息中的 <ppt_template> 块，为空时按默认版式生成。
        返回 JSON 字符串，包含 file_id、download_url、slide_count、title。
        注意：生成成功后只需简短告知用户"PPT 已生成，点击下载卡片即可下载"，
        不要重复输出 PPT 的大纲或详细内容。
        """
        if ppt_service is None:
            return "PPT 服务未初始化，请稍后再试"
        if note_service is None:
            return "笔记服务未就绪，请稍后再试"
        if db_session_factory is None:
            return "数据库会话不可用，请稍后再试"

        ids = [i.strip() for i in note_ids.split(",") if i.strip()]
        if not ids:
            return "未提供笔记 ID，请先选中要生成 PPT 的笔记"

        try:
            async with db_session_factory() as db:
                return await ppt_service.generate(
                    db=db,
                    user_id=user_id,
                    note_ids=ids,
                    title=title,
                    style=style,
                    template_id=template_id,
                    note_service=note_service,
                )
        except Exception as e:
            # 捕获所有异常返回友好提示，防止异常冒泡导致 Agent 循环中断
            logger.error(f"PPT 生成失败: user={user_id[:8]}, error={e}", exc_info=True)
            # 审查 M15：PptError 的 message 面向用户可透传，内部异常只给类别级提示
            from app.services.ppt_service import PptError
            if isinstance(e, PptError):
                return f"PPT 生成失败：{e}"
            return "PPT 生成失败，请稍后重试或检查笔记内容"

    @tool
    async def text_to_speech(text: str, voice: str = "zh-CN-XiaoxiaoNeural") -> str:
        """将文本转换为语音 MP3 文件（生成后可通过下载链接获取，用于笔记朗读、语言学习）。
        适用场景：用户说「朗读 / 读给我听 / 转成语音 / 转成音频」，尤其结合笔记朗读。
        **重要**：text 必须是【要朗读的完整内容】（如笔记全文或完整段落），不要只传标题、
        篇目名或几个词——否则生成的语音只有一两秒，用户无法收听。
        如果用户未提供具体内容且未引用任何笔记（如仅说"朗读一下"），【不要调用本工具】，
        应回复用户请其指定要朗读的笔记或内容。
        返回 JSON 字符串，包含 file_id、audio_url、duration_estimate。
        注意：生成成功后只需简短告知用户「语音已生成，点击下载卡片即可收听」。

        Args:
            text: 要转换的完整文本（最多 2000 字符/次，超出自动截断并提示）
            voice: 语音名称（默认 zh-CN-XiaoxiaoNeural；英文可选 en-US-JennyNeural）
        """
        try:
            import edge_tts
        except ImportError:
            return "语音服务不可用（未安装 edge-tts），请改用文字阅读"
        truncated = len(text) > 2000  # 保护上下文窗口（截断前记录原始长度）
        text = text[:2000]
        if not text.strip():
            return "文本内容为空，无法生成语音"

        # 按用户目录隔离存储（与 ppt_router 安全模型一致：音频含笔记内容，不走公开目录）
        from app.routers.tts_router import TTS_FILE_ROOT
        from app.ai_service.tool_results import artifact_output_id

        file_id = artifact_output_id.get() or uuid.uuid4().hex
        user_tts_dir = Path(TTS_FILE_ROOT) / user_id
        user_tts_dir.mkdir(parents=True, exist_ok=True)
        output_file = user_tts_dir / f"{file_id}.mp3"
        temporary = output_file.with_suffix('.mp3.part')
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(temporary))
            temporary.replace(output_file)
        except Exception as e:
            # 捕获所有异常返回友好提示，防止异常冒泡导致 Agent 循环中断
            logger.error(f"TTS 生成失败: user={user_id[:8]}, error={e}")
            return f"语音生成失败：{e}。请稍后重试或改用文字阅读。"
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning('TTS 临时文件将在定时清理中回收')
        duration = f"~{max(len(text) // 10, 1)}s"
        if truncated:
            duration += "（内容超长已截断）"

        # 生成后按用户目录 TTL/配额清理（与 PPT._cleanup 对称，防磁盘膨胀；
        # 定时任务兜底覆盖生成中断场景，见 scheduler.cleanup_orphan_tts_files）
        try:
            import asyncio
            from app.routers.tts_router import cleanup_user_tts_files
            await asyncio.to_thread(cleanup_user_tts_files, user_id)
        except Exception as e:
            logger.warning(f"TTS 用户目录清理失败: {e}")

        return json.dumps({
            "file_id": file_id,
            "audio_url": f"/api/v1/tts/{file_id}",
            "duration_estimate": duration,
        })

    # 全部工具注册表（名称 → 工具对象）
    all_tools = {
        "what_time_is_now": what_time_is_now,
        "get_user_info_tools": get_user_info_tools,
        "search_notes_tool": search_notes_tool,
        "get_note_content_tool": get_note_content_tool,
        "get_note_stats_tool": get_note_stats_tool,
        "get_today_reviews_tool": get_today_reviews_tool,
        "mark_reviewed_tool": mark_reviewed_tool,
        "create_note_tool": create_note_tool,
        "update_note_tool": update_note_tool,
        "get_related_notes_tool": get_related_notes_tool,
        "send_email": send_email,
        "generate_ppt_tool": generate_ppt_tool,
        # 外部 API 工具（直接集成，外部 API 工具接入文档 §4）：模块级 @tool 直接引用；
        # text_to_speech 为闭包（绑定 user_id，音频按用户目录隔离）
        "translate_text": translate_text,
        "wolfram_calculate": wolfram_calculate,
        "text_to_speech": text_to_speech,
    }

    # ===== 工具组解析（P2-6 ToolRegistry）=====
    # 组定义与动态工具（MCP）统一由注册表解析；内置工具实例仍在本函数内创建（绑定用户上下文）
    init_tool_registry()

    for group_name in groups or []:
        if group_name not in tool_registry.all_groups():
            logger.warning(f"工具组 '{group_name}' 未在 agent.yaml 中定义，跳过")

    # 组名列表 → 工具名列表（None 表示全部组，向后兼容）
    resolved_names = restrict_background_tools(tool_registry.resolve_names(groups))

    result = []
    for name in resolved_names:
        if name in all_tools:
            result.append(all_tools[name])
        else:
            dyn = tool_registry.get_dynamic(name)
            if dyn is not None:
                result.append(dyn)  # MCP 动态工具（P2-6 预留）
            else:
                logger.warning(f"工具路由: 工具 '{name}' 未注册，跳过")

    # An explicit empty or unknown selection grants no tools.

    logger.debug(f"工具按需加载: groups={groups}, 选中 {len(result)} 个工具（注册表解析）")
    return result
