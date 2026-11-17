"""
PPT 生成服务（设计方案 §4.3）

生成管线：
① 参数校验与上限（max_notes）
①' 读取用户 PPT 模板（可选；ppt_template_service 构造注入，方案 A 见 §6.5）
② 批量读取笔记（NoteService.get_notes_by_ids：权限校验 + 输入顺序保持）
③ 内容截断与组装上下文（每篇 ≤ per_note_chars，合计 ≤ total_context_chars）
④ qwen3-max 生成结构化大纲（JSON mode，内部超时 + 解析失败重试 + 纯文本降级）
⑤ 渲染引擎生成 .pptx 字节流（同步库 → 线程池，§5.5）
⑥ 落盘 + 元数据 sidecar + TTL/配额清理（§6.1）

模型获取：不持有模型引用，generate() 内函数内延迟导入
（与 chat.py 函数内导入模式一致，见 §4.3「LLM 获取路径」）。
"""
from app.model_config.runtime import model_for, template_for
from app.model_config.snapshots import current_snapshot
import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from langchain_core.messages import HumanMessage

from app.core.logger_handler import logger

# 生成文件存储根目录（相对项目根，与 ppt_router 共享）
PPT_FILE_ROOT = os.getenv("PPT_FILE_ROOT", os.path.join("data", "ppt"))

# 大纲 JSON 解析失败时使用的行首要点提取上限
_MAX_FALLBACK_BULLETS = 5


class PptError(Exception):
    """PPT 生成业务异常（消息面向用户）"""


def load_ppt_config() -> dict:
    """加载 PPT 功能配置（config/ppt.yaml 的 ppt 段）"""
    from app.utils.config import get_ppt_config
    return get_ppt_config()


class PptService:
    """PPT 生成服务（轻量对象：只读配置 + 目录初始化，阶段 0 同步创建）"""

    def __init__(self, config: Optional[dict] = None, ppt_template_service=None):
        """
        Args:
            config: config/ppt.yaml 的 ppt 段（§8.2）
            ppt_template_service: 模板服务实例（方案 A 构造注入，§6.5；
                阶段 0 与 PptService 同生命周期，Phase 1.5 实现）
        """
        self.config = config or {}
        self.ppt_template_service = ppt_template_service
        os.makedirs(PPT_FILE_ROOT, exist_ok=True)

    # ========== 工具函数 ==========

    def _user_dir(self, user_id: str) -> Path:
        return Path(PPT_FILE_ROOT) / user_id

    def _error(self, msg: str) -> str:
        """错误返回：纯文本（非 JSON）——stream.py 的 tool_file 解析会跳过，
        LLM 在文本回复中向用户说明原因（§6.3 兜底逻辑）"""
        return msg

    def _get_outline_model(self):
        """独立的大纲生成模型（JSON mode，§4.3 LLM 获取路径）。

        复用 DASHSCOPE_BASE_URL / DASHSCOPE_API_KEY（factory.py:322/344），
        不依赖 init_manager 的对话模型实例（避免 thinking 开关干扰 JSON 模式）。
        """
        if current_snapshot() is not None:
            return model_for('ppt_outline')
        from langchain_openai import ChatOpenAI

        from app.utils.factory import DASHSCOPE_BASE_URL

        model_name = os.getenv("DASHSCOPE_CHAT_MODEL", "qwen3-max")
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        outline_timeout = self.config.get("outline_timeout", 45)
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=DASHSCOPE_BASE_URL,
            streaming=False,
            request_timeout=outline_timeout + 5,
            # DashScope OpenAI 兼容端点：json_object 模式
            # （prompt 须含小写 "json" 字样，见 _generate_outline）
            model_kwargs={"response_format": {"type": "json_object"}},
            # 大纲生成是结构化任务，无需思考：关闭 thinking 提速约 3 倍
            # （实测默认 7.1s → 2.5s；长 prompt 40s → 预计 15s 内）
            extra_body={"enable_thinking": False},
        )

    # ========== 生成管线 ==========

    async def generate(
        self, db, user_id: str, note_ids: List[str],
        title: str = "", style: str = "business",
        template_id: str = "", note_service=None,
    ) -> str:
        """
        生成讲解 PPT（返回 JSON 字符串，供工具直接返回给 LLM）

        Args:
            db: 数据库会话（由工具经 db_session_factory 创建）
            user_id: 当前用户 ID
            note_ids: 笔记 ID 列表（须来自用户引用笔记，工具已强校验归属）
            title: PPT 主题（可选，默认由 LLM 拟定）
            style: 风格预设 business / academic / minimal
            template_id: 用户模板 ID（可选，§6.5；为空时默认版式）
            note_service: NoteService 实例（由工具闭包透传）

        Returns:
            JSON 字符串（file_id / download_url / slide_count / title / note_count）
        """
        # ① 参数校验与上限
        max_notes = self.config.get("max_notes", 10)
        if not note_ids or not 1 <= len(note_ids) <= max_notes:
            return self._error(f"一次最多选择 {max_notes} 篇笔记")
        if style not in ("business", "academic", "minimal"):
            style = "business"

        # ①' 读取用户 PPT 模板（可选；方案 A 构造注入，§6.5）
        template_path = None
        if template_id:
            template_path = await self._resolve_template(db, user_id, template_id)

        # ② 批量读取笔记（权限校验 + 顺序保持；静默忽略无效 ID）
        if note_service is None:
            return self._error("笔记服务未就绪，请稍后重试")
        notes = await note_service.get_notes_by_ids(db, note_ids, user_id)
        if not notes:
            return self._error("未找到对应笔记，请确认所选笔记未被删除")

        # ③ 内容截断与组装上下文（§5.4）
        context = self._build_context(notes)

        # ④ 生成结构化大纲（JSON mode；内部超时 + 解析失败重试 + 纯文本降级）
        outline = await self._generate_outline(context, title, style, notes)

        # ⑤ 渲染引擎生成 .pptx 字节流（同步库 → 线程池，§5.5；引擎由 PPT_ENGINE 切换，§8.3）
        from app.services.ppt_renderer import create_renderer

        renderer = create_renderer(self.config)
        pptx_bytes = await asyncio.to_thread(
            renderer.render, outline, theme=style, template_path=template_path)

        # ⑥ 落盘 + 元数据 + TTL/配额清理
        file_id = self._save_file(user_id, outline.title, pptx_bytes,
                                  slide_count=len(outline.slides))
        return json.dumps({
            "file_id": file_id,
            "download_url": f"/api/v1/ppt/{file_id}",
            "slide_count": len(outline.slides),
            "title": outline.title,
            "note_count": len(notes),
        }, ensure_ascii=False)

    async def _resolve_template(
        self, db, user_id: str, template_id: str
    ) -> Optional[str]:
        """归属校验 + 返回模板路径；任何失败 → None（降级默认版式，不阻断生成）"""
        if self.ppt_template_service is None:
            logger.warning(f"PPT 模板服务未初始化，忽略模板 template_id={template_id}")
            return None
        try:
            return await self.ppt_template_service.resolve_template_path(
                db, user_id, template_id)
        except Exception as e:
            logger.warning(f"PPT 模板解析失败，降级默认版式: {e}")
            return None

    # ========== 上下文组装（§5.4） ==========

    def _build_context(self, notes) -> str:
        """组装 LLM 上下文：每篇 ≤ per_note_chars，合计 ≤ total_context_chars"""
        per_note = self.config.get("per_note_chars", 2000)
        total_cap = self.config.get("total_context_chars", 12000)
        parts: List[str] = []
        total = 0
        for note in notes:
            content = self._truncate_note(note.content or "", per_note)
            part = f"### 笔记标题：{note.title}\n{content}"
            if total + len(part) > total_cap:
                remain = total_cap - total
                if remain > 200:
                    parts.append(part[:remain].rstrip() + "\n……（已省略）……")
                break
            parts.append(part)
            total += len(part)
        return "\n\n".join(parts)

    @staticmethod
    def _truncate_note(content: str, limit: int) -> str:
        """单篇截断：保留标题层级/列表/首段与结论；代码块超 40 行折叠（§5.4）"""
        if len(content) <= limit:
            return content
        kept: List[str] = []
        budget = limit
        in_code = False
        code_lines = 0
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                code_lines = 0
                if len(line) > budget:
                    break
                kept.append(line)
                budget -= len(line)
                continue
            if in_code:
                code_lines += 1
                if code_lines > 40:
                    kept.append("……（代码已省略）……详见笔记")
                    in_code = False
                    continue
                if len(line) > budget:
                    break
                kept.append(line)
                budget -= len(line)
                continue
            if len(line) > budget:
                break
            kept.append(line)
            budget -= len(line)
        return "\n".join(kept).rstrip() + "\n……（已省略）……详见笔记"

    # ========== 大纲生成（§4.3 步骤 ④） ==========

    async def _generate_outline(self, context: str, title: str, style: str, notes):
        """LLM 生成结构化大纲；解析失败重试 outline_retries 次；仍失败 → 纯文本降级"""
        from app.schemas.ppt import PPTOutline

        max_slides = self.config.get("max_slides", 20)
        prompt = (
            "你是 PPT 大纲设计专家。请根据给定的笔记内容，生成一份讲解用 PPT 的结构化大纲。\n"
            "要求：\n"
            "1. 仅基于笔记内容，不要编造笔记中不存在的信息；\n"
            "2. 每篇笔记默认对应 1 个 section 章节页 + 1~3 个 content 内容页；\n"
            "3. 输出必须是严格的 json 对象（不要 markdown 代码块包裹），结构为：\n"
            '{"title": string, "subtitle": string, "style": "business|academic|minimal", '
            '"slides": [{"type": "cover|agenda|section|content|summary", "title": string, '
            '"subtitle": string 或 null, "items": string[] 或 null, "bullets": string[] 或 null, '
            '"code": string 或 null, "notes": string 或 null}]}\n'
            "4. slides 第一页必须是 cover，最后一页必须是 summary，且至少包含一个 content 页；\n"
            f"5. 总页数不超过 {max_slides} 页；content 页的 bullets 写 3~6 条要点。\n\n"
            f"PPT 主题：{title or '（根据笔记内容拟定）'}\n"
            f"风格预设：{style}\n\n"
            f"笔记内容：\n{context}"
        )
        outline_timeout = self.config.get("outline_timeout", 45)
        prompt=template_for('ppt_outline',prompt,content=context,title=title or '',requirements=style,user_message=context)
        retries = self.config.get("outline_retries", 1)
        if current_snapshot() is not None:
            retries=0
        last_error: Optional[Exception] = None

        for attempt in range(retries + 1):
            # ① LLM 调用（超时 → 不重试直接降级，§8.4：响应慢不触发重试；
            #    调用异常 → 重试）
            try:
                async with asyncio.timeout(outline_timeout):
                    model = self._get_outline_model()
                    resp = await model.ainvoke([HumanMessage(content=prompt)])
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"大纲生成超时（{outline_timeout}s）")
                logger.warning(f"PPT 大纲生成超时（{outline_timeout}s），直接降级")
                break
            except Exception as e:
                last_error = e
                logger.warning(f"PPT 大纲调用失败（第 {attempt + 1}/{retries + 1} 次）: {e}")
                continue

            # ② JSON 解析 + Pydantic 校验（解析失败 → 重试；仍失败 → 降级）
            try:
                raw = resp.content if isinstance(resp.content, str) else str(resp.content)
                return self._parse_outline_json(raw)
            except Exception as e:
                last_error = e
                logger.warning(f"PPT 大纲解析失败（第 {attempt + 1}/{retries + 1} 次）: {e}")

        # 降级：纯文本大纲按章节模板渲染（§10）
        logger.warning(f"PPT 大纲生成失败，降级纯文本大纲: {last_error}")
        return self._fallback_outline(title or "讲解PPT", notes)

    @staticmethod
    def _parse_outline_json(raw: str):
        """解析 LLM 输出的 JSON 大纲（容忍 markdown 代码块包裹/前后杂讯，§10）"""
        import json

        from app.schemas.ppt import PPTOutline

        text = raw.strip()
        try:
            return PPTOutline.model_validate_json(text)
        except Exception:
            pass
        # 提取第一个 { ... } 块再解析
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("LLM 输出中未找到 JSON 对象")
        data = json.loads(text[start:end + 1])
        return PPTOutline.model_validate(data)

    def _fallback_outline(self, title: str, notes):
        """纯文本大纲降级（§10）：封面 + 目录 + 每篇笔记章节/内容页 + 总结

        防御：notes 为空时（异常场景）补一个通用内容页，保证 PPTOutline
        结构约束（必须含 content 页）始终满足。
        """
        from app.schemas.ppt import PPTOutline, PPTSlide

        slides = [
            PPTSlide(type="cover", title=title, subtitle="AI 自动生成（大纲降级模式）"),
        ]
        if len(notes) > 1:
            slides.append(PPTSlide(
                type="agenda", title="目录",
                items=[n.title for n in notes]))
        if notes:
            for note in notes:
                slides.append(PPTSlide(type="section", title=note.title))
                slides.append(PPTSlide(
                    type="content", title=note.title,
                    bullets=self._first_bullets(note.content or "")))
        else:
            slides.append(PPTSlide(
                type="content", title="内容摘要",
                bullets=["大纲生成失败，已降级为纯文本模式"]))
        slides.append(PPTSlide(type="summary", title="总结", bullets=[
            "以上内容来自所选笔记", "建议重新生成以获得更完善的大纲"]))
        return PPTOutline(title=title, subtitle="AI 自动生成", slides=slides)

    @staticmethod
    def _first_bullets(content: str, limit: int = _MAX_FALLBACK_BULLETS) -> List[str]:
        """提取笔记行首要点（降级大纲用）"""
        bullets: List[str] = []
        for line in content.splitlines():
            stripped = line.strip().lstrip("#*- ")
            if stripped and len(stripped) > 6 and len(bullets) < limit:
                bullets.append(stripped[:60])
        return bullets or ["详见笔记内容"]

    # ========== 落盘与清理（§6.1） ==========

    def _save_file(self, user_id: str, title: str, pptx_bytes: bytes,
                   slide_count: Optional[int] = None) -> str:
        from app.ai_service.tool_results import artifact_output_id

        file_id = artifact_output_id.get() or uuid.uuid4().hex
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        pptx_path = user_dir / f"{file_id}.pptx"
        pptx_path.write_bytes(pptx_bytes)

        meta = {
            "title": title or "讲解PPT",
            "slide_count": slide_count,
            "created_at": datetime.now().isoformat(),
            "size": len(pptx_bytes),
            "engine": self.config.get("engine", "python_pptx"),
        }
        (user_dir / f"{file_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        self._cleanup(user_dir)
        logger.info(f"PPT 已生成: file_id={file_id}, user={user_id[:8]}, "
                    f"slides={slide_count}, size={len(pptx_bytes)}")
        return file_id

    def _cleanup(self, user_dir: Path) -> None:
        """TTL 过期清理 + 每用户文件数配额（防磁盘膨胀，§6.1）"""
        ttl_hours = self.config.get("file_ttl_hours", 24)
        max_files = self.config.get("max_files_per_user", 20)
        now = datetime.now()
        deadline = now - timedelta(hours=ttl_hours)

        entries = []  # (created_at, file_id)
        for p in user_dir.glob("*.json"):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(meta.get("created_at", ""))
            except Exception:
                created = now  # 元数据损坏：不因 TTL 误删，按配额兜底
            entries.append((created, p.stem))

        # ① TTL 过期 → 删除
        for created, stem in entries:
            if created < deadline:
                self._remove_file(user_dir, stem)
                logger.info(f"PPT TTL 清理: {stem}")

        # ② 配额超限 → 保留最新 max_files 个，删最旧
        alive = sorted((c, s) for c, s in entries if c >= deadline)
        for _, stem in alive[:max(0, len(alive) - max_files)]:
            self._remove_file(user_dir, stem)
            logger.info(f"PPT 配额清理: {stem}")

    @staticmethod
    def _remove_file(user_dir: Path, stem: str) -> None:
        try:
            (user_dir / f"{stem}.pptx").unlink(missing_ok=True)
            (user_dir / f"{stem}.json").unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"PPT 文件清理失败: {e}")
