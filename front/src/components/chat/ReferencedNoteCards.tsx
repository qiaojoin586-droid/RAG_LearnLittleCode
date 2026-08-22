/**
 * 引用笔记卡片（v2 卡片化改造：用户消息侧）
 *
 * 用户消息中引用的笔记以卡片形式展示（与 AI 搜索结果卡片同族设计）：
 * - 左侧：绿色系线性文档图标
 * - 右侧：加粗完整标题 + 正文摘要（前 50 字省略号）
 * - hover：背景加深 + 阴影增强 + 手型光标
 * - 点击卡片：页面中央弹出「笔记详情预览弹窗」展示完整正文
 *
 * 正文直接取自消息文本（引用块内已携带完整笔记内容），无需额外请求；
 * 预览弹窗通过 createPortal 挂到 document.body（浮层 transform 定位上下文
 * 会把 fixed 弹窗困在浮层内，历史教训：ConfirmDialog 同因）。
 */

import { useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { FileText, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import { noteExcerpt } from '../../utils/noteFormat';
import { escapeInlineHtml } from '../../utils/markdownCompat';
import type { ReferencedNote } from '../../utils/noteReferenceParser';

export default function ReferencedNoteCards({ notes }: { notes: ReferencedNote[] }) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<ReferencedNote | null>(null);

  return (
    <div className="flex flex-col gap-2.5 my-3">
      {notes.map((note, idx) => (
        /* 卡片：浅底圆角 + 绿色文档图标 + 标题 + 摘要 */
        <div
          key={note.id ?? `${note.title}-${idx}`}
          onClick={() => setPreview(note)}
          className="flex items-start gap-3 p-3 rounded-[var(--radius-md)] cursor-pointer
            bg-[var(--color-card)] border border-[var(--color-border)]
            hover:bg-[var(--color-accent-bg)] hover:shadow-[var(--shadow-accent-md)]
            transition-all duration-200"
        >
          <FileText size={18} className="text-[var(--color-success)] shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <span className="text-sm font-semibold text-[var(--color-text)] truncate block">
              {note.title}
            </span>
            {note.content && (
              <p className="text-xs text-[var(--color-text-secondary)] mt-1 break-words">
                {noteExcerpt(note.content, 50)}
              </p>
            )}
          </div>
        </div>
      ))}

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
              {/* 头部：完整标题 + 关闭按钮 */}
              <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-[var(--color-border)] shrink-0">
                <h3 className="font-heading text-base font-bold text-[var(--color-text)] truncate">
                  {preview.title}
                </h3>
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
                {preview.content ? (
                  <div className="md-prose">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, remarkMath]}
                      rehypePlugins={[rehypeHighlight, rehypeKatex]}
                    >
                      {/* 转义行内角括号：避免泛型 <T> 被当作原始 HTML 丢弃 */}
                      {escapeInlineHtml(preview.content)}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-sm text-[var(--color-text-secondary)]">
                    {t('note.previewNoContent')}
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
