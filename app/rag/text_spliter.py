"""
文档与笔记共用的切片器。每个新切片保留原文字符区间，
Markdown 还记录章节及表格、代码块边界。
"""

import re
from typing import List, Tuple

from app.core.logger_handler import logger
from app.utils.config import get_chroma_config


class TextSplitter:
    """
    文本切片器

    文本优先按段落或句子切分，Markdown 按块切分，PDF 由调用方逐页传入。
    历史文档重建仍可复用原有分割器。独立的语义合并方法仅处理文本列表。
    """

    def __init__(self, embed_model=None):
        """
        初始化切片器

        Args:
            embed_model: 可选的 embedding 模型实例（用于语义合并）
        """
        config = get_chroma_config()
        splitter_config = config.get("text_splitter", {})

        self.chunk_size = splitter_config.get("chunk_size", 1000)
        self.chunk_overlap = splitter_config.get("chunk_overlap", 200)
        self.merge_threshold = splitter_config.get("merge_similarity_threshold", 0.85)
        self.embed_model = embed_model

    # ================================================================
    # 公开 API
    # ================================================================

    def split_text(self, text: str, doc_type: str = "txt") -> List[str]:
        """
        同步分割文本，返回纯文本切片列表（向后兼容）

        Args:
            text:     待分割的文本
            doc_type: 文档类型（"md" / "txt" / "pdf"）

        Returns:
            切片文本列表
        """
        return [content for content, _ in self.split_with_positions(text, doc_type)]

    def split_with_sections(self, text: str, doc_type: str = "txt") -> Tuple[List[str], List[dict]]:
        chunks = self.split_with_positions(text, doc_type)
        return ([content for content, _ in chunks],
                [{**span, 'chunk_index': i} for i, (_, span) in enumerate(chunks)])

    def split_with_positions(self, text: str, doc_type: str) -> list[tuple[str, dict]]:
        """Slice the exact source within each structural region and size limit."""
        from app.rag.structure import markdown_regions, region_spans

        regions = markdown_regions(text) if doc_type.lower().removeprefix('text/') in ('md', 'markdown') else [
            (0, len(text), '', 'text')]
        chunks = []
        for start, end, title, kind in regions:
            for left, right in region_spans(text, start, end, kind, self.chunk_size, self.chunk_overlap):
                span = {'section_title': title, 'block_kind': kind, 'char_start': left, 'char_end': right}
                if kind in ('code', 'table'):
                    span.update(block_start=start, block_end=end)
                chunks.append((text[left:right], span))
        return chunks

    def split_legacy_positions(self, text: str, doc_type: str) -> list[tuple[str, dict]]:
        """Locate each excerpt in its original section using Python character offsets."""
        sections = self._parse_markdown_sections(text) if doc_type in ('md', 'markdown') else []
        splitter = self._generic_splitter(add_start_index=True)
        positions = []
        for section in sections or [{'start': 0, 'end': len(text), 'title': ''}]:
            base = section['start']
            for chunk in splitter.create_documents([text[base:section['end']]]):
                offset = chunk.metadata['start_index']
                content = chunk.page_content
                if offset < 0 or text[base + offset:base + offset + len(content)] != content:
                    raise ValueError('Could not locate the document excerpt')
                positions.append((content, {'section_title': section['title'], 'char_start': base + offset,
                                            'char_end': base + offset + len(content)}))
        return positions

    async def async_split_text(
        self, text: str, doc_type: str = "txt"
    ) -> List[str]:
        """异步分割文本（线程池包装，向后兼容）"""
        import asyncio
        loop = asyncio.get_event_loop()
        import contextvars
        return await loop.run_in_executor(None, contextvars.copy_context().run,self.split_text, text, doc_type)

    async def async_split_with_sections(
        self, text: str, doc_type: str = "txt"
    ) -> Tuple[List[str], List[dict]]:
        """异步分割文本，返回内容及章节元数据"""
        import asyncio
        loop = asyncio.get_event_loop()
        import contextvars
        return await loop.run_in_executor(None,contextvars.copy_context().run,self.split_with_sections,text,doc_type)

    def merge_similar_chunks(self, chunks: List[str]) -> List[str]:
        """
        使用 embedding 做真正的语义合并

        计算相邻片段的 embedding 余弦相似度，超过 merge_threshold 则合并。
        需要初始化时传入 embed_model，否则直接返回原列表。

        Args:
            chunks: 原始切片列表

        Returns:
            合并后的切片列表
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.errors import fail
            fail(50305,'SYNC_SEMANTIC_MERGE_UNSUPPORTED',503)
        if not chunks or len(chunks) < 2:
            return chunks

        if self.embed_model is None:
            logger.debug("未配置 embed_model，跳过语义合并")
            return chunks

        try:
            embeddings = self.embed_model.embed_documents(chunks)

            merged_texts = [chunks[0]]
            merged_embs = [embeddings[0]]

            for i in range(1, len(chunks)):
                sim = self._cosine_similarity(merged_embs[-1], embeddings[i])

                if sim >= self.merge_threshold:
                    merged_texts[-1] = merged_texts[-1] + "\n\n" + chunks[i]
                    # 合并后的 embedding 取移动平均
                    merged_embs[-1] = [
                        (a + b) / 2
                        for a, b in zip(merged_embs[-1], embeddings[i])
                    ]
                else:
                    merged_texts.append(chunks[i])
                    merged_embs.append(embeddings[i])

            if len(merged_texts) < len(chunks):
                logger.info(
                    f"语义合并: {len(chunks)} → {len(merged_texts)} 个片段 "
                    f"(阈值={self.merge_threshold})"
                )

            return merged_texts

        except Exception as e:
            logger.warning(f"语义合并失败，返回原始切片: {e}")
            return chunks

    # ================================================================
    # 文档类型路由
    # ================================================================

    # Reproduce pre-manifest document boundaries when rebuilding historical indexes.
    _HEADING_RE = re.compile(r'^(#{1,4})\s+(.+)$', re.MULTILINE)

    def _parse_markdown_sections(self, text: str) -> List[dict]:
        """
        扫描 Markdown 标题行，将文本划分为章节区段

        Returns:
            [{"start": int, "end": int, "title": str, "level": int}, ...]
            title 格式如 "## 安装指南"
        """
        heading_positions = []  # [(char_position, level, title), ...]

        for m in self._HEADING_RE.finditer(text):
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_positions.append((m.start(), level, title))

        if not heading_positions:
            return []

        sections = []
        for i, (pos, level, title) in enumerate(heading_positions):
            end = (
                heading_positions[i + 1][0]
                if i + 1 < len(heading_positions)
                else len(text)
            )
            sections.append({
                "start": pos,
                "end": end,
                "title": f"{'#' * level} {title}",
                "level": level,
            })

        # 如果正文在第一个标题之前，作为无标题前言
        if heading_positions and heading_positions[0][0] > 0:
            preamble = text[:heading_positions[0][0]].strip()
            if preamble:
                sections.insert(0, {
                    "start": 0,
                    "end": heading_positions[0][0],
                    "title": "",
                    "level": 0,
                })

        return sections

    # ================================================================
    # 通用递归字符分割
    # ================================================================

    def _generic_splitter(self, *, add_start_index=False):
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        return RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            separators=[
                "\n\n\n",           # 多空行分隔的章节
                "\n\n",             # 段落
                "\n",               # 单换行
                "。",               # 中文句号
                ". ",               # 英文句号 + 空格
                "！", "!",          # 感叹号
                "？", "?",          # 问号
                "；", ";",          # 分号
                "，", ",",          # 逗号
                " ",                # 空格（最终兜底）
            ],
            length_function=len,
            add_start_index=add_start_index,
        )

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """计算两个向量的余弦相似度"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0

        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = (sum(a * a for a in vec_a)) ** 0.5
        norm_b = (sum(b * b for b in vec_b)) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
