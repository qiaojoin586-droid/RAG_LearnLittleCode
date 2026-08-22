/**
 * 笔记搜索结果卡片（v2 卡片化改造）
 *
 * 将 AI 回复中的纯文本笔记搜索列表改为卡片展示（单条笔记一张卡片）：
 * - 左侧：绿色系线性文档图标
 * - 右侧两行：加粗完整标题 + 右对齐创建日期（YYYY/MM/DD）；正文摘要（前 50 字省略号）
 * - hover：背景加深 + 阴影增强 + 手型光标
 * - 点击卡片：页面中央弹出「笔记详情预览弹窗」（完整标题 + 创建/更新日期 + 完整正文滚动）
 *
 * 数据富化（不动后端，复用现有接口）：
 * - 条目携带 ID → notesApi.detail 拉取完整笔记（标题/日期/正文），模块级缓存防重复请求
 * - 无 ID → notesApi.list 按标题精确匹配兜底
 * - 均失败 → 以消息文本为准降级展示（日期隐藏，正文仅有摘要）
 *
 * 预览弹窗通过 createPortal 挂到 document.body：
 * 卡片位于 AI 浮层内部（transform 定位上下文），直接渲染 fixed 弹窗会被
 * 限制在浮层范围内，portal 后才是真正的"页面中央"（历史教训：ConfirmDialog 同因）。
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { FileText, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import { notesApi } from '../../api/notes';
import { noteExcerpt } from '../../utils/noteFormat';
import { escapeInlineHtml } from '../../utils/markdownCompat';
import type { Note } from '../../types/api';
import type { ParsedSearchResult } from '../../utils/noteSearchParser';

/** 笔记详情缓存：流式重解析/多卡片引用同一笔记时复用 */
const detailCache = new Map<string, Promise<Note>>();
function fetchCachedNote(id: string): Promise<Note> {
  let p = detailCache.get(id);
  if (!p) {
    p = notesApi.detail(id).then((res) => res.data.data);
    p.catch(() => {
      detailCache.delete(id); // 失败允许下次重试
    });
    detailCache.set(id, p);
  }
  return p;
}

/** YYYY/MM/DD */
function formatDate(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}/${mm}/${dd}`;
}

/** 标题匹配候选：原文 + 去掉尾部括号（AI 常追加日期等括号后缀，如"（2026年8月）"） */
function titleCandidates(rawTitle: string): string[] {
  const candidates = [rawTitle.trim()];
  const stripped = rawTitle.replace(/[（(][^）)]*[）)]\s*$/g, '').trim();
  if (stripped && stripped !== rawTitle.trim()) candidates.push(stripped);
  return candidates;
}

/** 按标题匹配笔记：精确 > 去括号后缀精确 > 双向包含（大小写不敏感） */
function matchNote(listNotes: Note[], rawTitle: string): Note | undefined {
  const candidates = titleCandidates(rawTitle);
  for (const c of candidates) {
    const exact = listNotes.find((n) => n.title.trim() === c);
    if (exact) return exact;
  }
  for (const c of candidates) {
    const cl = c.toLowerCase();
    const loose = listNotes.find(
      (n) =>
        n.title.trim().toLowerCase().includes(cl) ||
        cl.includes(n.title.trim().toLowerCase())
    );
    if (loose) return loose;
  }
  return undefined;
}

interface EnrichedItem {
  raw: ParsedSearchResult;
  note: Note | null;
}

export default function SearchResultCards({ results }: { results: ParsedSearchResult[] }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<EnrichedItem[]>(() => results.map((raw) => ({ raw, note: null })));
  const [preview, setPreview] = useState<EnrichedItem | null>(null);

  // 富化数据：有 ID 拉详情；无 ID 按标题在列表精确匹配兜底
  // 以结果签名做依赖（流式中新增条目时重新富化；detailCache 防重复请求）
  const signature = results.map((r) => `${r.noteId ?? ''}#${r.title}`).join('|');
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const withId = results.filter((r) => r.noteId);
      const withoutId = results.filter((r) => !r.noteId);

      const idNotes = new Map<string, Note>();
      await Promise.all(
        withId.map(async (r) => {
          try {
            idNotes.set(r.noteId!, await fetchCachedNote(r.noteId!));
          } catch {
            // 详情失败保持文本兜底
          }
        })
      );

      let listNotes: Note[] = [];
      if (withoutId.length > 0) {
        try {
          const res = await notesApi.list({ page: 1, page_size: 100 });
          listNotes = res.data?.data?.notes ?? [];
        } catch {
          // 列表失败保持文本兜底
        }
      }

      if (cancelled) return;
      setItems(
        results.map((raw) => {
          let note = raw.noteId ? idNotes.get(raw.noteId) : undefined;
          if (!note) note = matchNote(listNotes, raw.title);
          return { raw, note: note ?? null };
        })
      );
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  const previewNote = preview?.note ?? null;

  return (
    <div className="flex flex-col gap-2.5 my-3">
      {items.map((item) => {
        const title = item.note?.title ?? item.raw.title;
        const excerpt = item.note
          ? noteExcerpt(item.note.content, 50)
          : noteExcerpt(item.raw.excerpt, 50);
        const date = formatDate(item.note?.created_at ?? undefined);
        return (
          /* 卡片：浅底圆角 + 绿色文档图标 + 标题/日期 + 摘要 */
          <div
            key={`${item.note?.id ?? ''}-${item.raw.title}`}
            onClick={() => setPreview(item)}
            className="flex items-start gap-3 p-3 rounded-[var(--radius-md)] cursor-pointer
              bg-[var(--color-card)] border border-[var(--color-border)]
              hover:bg-[var(--color-accent-bg)] hover:shadow-[var(--shadow-accent-md)]
              transition-all duration-200"
          >
            <FileText size={18} className="text-[var(--color-success)] shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-semibold text-[var(--color-text)] truncate">{title}</span>
                {date && (
                  <span className="text-[11px] text-[var(--color-text-tertiary)] shrink-0">{date}</span>
                )}
              </div>
              {excerpt && (
                <p className="text-xs text-[var(--color-text-secondary)] mt-1 break-words">{excerpt}</p>
              )}
            </div>
          </div>
        );
      })}

      {/* 笔记详情预览弹窗（portal 到 body，页面居中，层级高于对话界面） */}
      {preview &&
        createPortal(
          <div
            className="fixed inset-0 z-[200] flex items-center justify-center p-4 bg-black/40 animate-fade-in"
            onClick={() => setPreview(null)}
          >
            <div
              className="w-[560px] max-w-[92vw] max-h-[80vh] flex flex-col rounded-[var(--radius-lg)]
                bg-[var(--color-card)] border border-[var(--color-border)] shadow-[var(--shadow-dialog)] animate-slide-up"
              onClick={(e) => e.stopPropagation()}
            >
              {/* 头部：完整标题 + 创建/更新日期 + 关闭按钮 */}
              <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-[var(--color-border)] shrink-0">
                <div className="min-w-0">
                  <h3 className="font-heading text-base font-bold text-[var(--color-text)] truncate">
                    {preview.note?.title ?? preview.raw.title}
                  </h3>
                  {previewNote && (
                    <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
                      {t('note.previewCreated')} {formatDate(previewNote.created_at)}
                      {previewNote.updated_at &&
                        ` · ${t('note.previewUpdated')} ${formatDate(previewNote.updated_at)}`}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => setPreview(null)}
                  className="p-1.5 rounded-lg text-[var(--color-text-tertiary)]
                    hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-text)]
                    transition-colors shrink-0"
                  title={t('assistant.close')}
                >
                  <X size={18} />
                </button>
              </div>

              {/* 主体：完整正文（超出高度纵向滚动） */}
              <div className="flex-1 overflow-y-auto px-5 py-4 min-h-0">
                {previewNote?.content ? (
                  <div className="md-prose">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, remarkMath]}
                      rehypePlugins={[rehypeHighlight, rehypeKatex]}
                    >
                      {/* 转义行内角括号：避免泛型 <T> 被当作原始 HTML 丢弃 */}
                      {escapeInlineHtml(previewNote.content)}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-sm text-[var(--color-text-secondary)] whitespace-pre-wrap break-words">
                    {preview.raw.excerpt || t('note.previewNoContent')}
                  </p>
                )}
              </div>
            </div>
          </div>,
          document.body
        )}
    </div>
  );
}
