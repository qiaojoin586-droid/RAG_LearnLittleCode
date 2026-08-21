/**
 * 笔记竖排卡片（v2 笔记页三栏第二栏，融合 test.md 字段映射）
 *
 * 卡片从上到下：
 * - 右上角：收藏星星（is_pinned 切换，notesApi.update）+ 删除垃圾桶（软删除进回收站）
 * - 标题（加粗单行省略）+ MD 格式徽章（系统为 Markdown 笔记应用，统一显示 MD）
 * - 内容摘要（灰色小字，2 行省略）
 * - 浅青绿色标签（tags 前 3 个）
 * - 底部：分类名 + 发布日期
 *
 * 批量模式下卡片显示复选框，点击切换选中。
 */

import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import { Star, Trash2 } from 'lucide-react';
import type { Note } from '../../types/api';
import { detectNoteFormat, noteExcerpt } from '../../utils/noteFormat';

interface NoteCardItemProps {
  note: Note;
  /** 批量模式：显示复选框 */
  batchMode: boolean;
  isSelected: boolean;
  /** 收藏/取消收藏（父级调 notesApi.update 后更新列表） */
  onPin: (note: Note) => void;
  /** 删除（父级 ConfirmDialog 确认后 notesApi.delete） */
  onDelete: (note: Note) => void;
  /** 批量模式切换选中 */
  onToggleSelect: (id: string) => void;
  /** 点击卡片：跳转 /notes/:id 编辑器（决策 D4） */
  onOpen: (note: Note) => void;
}

/** 日期格式化：只显示日期部分 */
function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString();
}

/** React.memo 包裹：批量模式下仅选中态变化的卡片重渲染（阶段五性能优化） */
function NoteCardItem({
  note,
  batchMode,
  isSelected,
  onPin,
  onDelete,
  onToggleSelect,
  onOpen,
}: NoteCardItemProps) {
  const { t } = useTranslation();
  // 类型徽章读后端 format 字段（缺失/未知兜底 'md'）
  const format = detectNoteFormat(note.format);
  const pinned = note.is_pinned;

  return (
    <div
      className={`group relative bg-[var(--color-card)] rounded-[var(--radius-md)] p-4 border
        transition-all duration-200 hover:shadow-[var(--shadow-accent-lg)] cursor-pointer ${
        isSelected
          ? 'border-[var(--color-accent)] ring-1 ring-[var(--color-accent)]'
          : 'border-[var(--color-border)] shadow-[var(--shadow-card)]'
      }`}
      onClick={() => (batchMode ? onToggleSelect(note.id) : onOpen(note))}
    >
      {/* 批量模式复选框 */}
      {batchMode && (
        <div className="absolute top-3 right-3 z-10">
          <div
            className={`w-5 h-5 rounded border-2 flex items-center justify-center transition-colors ${
              isSelected
                ? 'bg-[var(--color-accent)] border-[var(--color-accent)]'
                : 'border-[var(--color-border)] bg-[var(--color-card)]'
            }`}
          >
            {isSelected && (
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                <path d="M2 6l3 3 5-5" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
          </div>
        </div>
      )}

      {/* 右上角：收藏星星 + 删除垃圾桶（hover 显示；已收藏的星星常显） */}
      {!batchMode && (
        <div
          className={`absolute top-3 right-3 flex gap-1 transition-opacity ${
            pinned ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
          }`}
        >
          <button
            onClick={(e) => {
              e.stopPropagation();
              onPin(note);
            }}
            className="p-1 rounded hover:bg-[var(--color-surface-alt)] transition-colors"
            title={t('note.pinToggle')}
          >
            <Star
              size={15}
              className={
                pinned
                  ? 'fill-[var(--color-warning)] text-[var(--color-warning)]'
                  : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-warning)]'
              }
            />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete(note);
            }}
            className="p-1 rounded text-[var(--color-text-tertiary)]
              hover:text-[var(--color-danger)] hover:bg-[var(--color-danger-bg)]
              transition-colors"
            title={t('common.delete')}
          >
            <Trash2 size={15} />
          </button>
        </div>
      )}

      {/* 标题行 + 格式徽章 */}
      <div className="flex items-center gap-2 mb-2 pr-14">
        <h3 className="text-sm font-semibold text-[var(--color-text)] truncate flex-1">{note.title}</h3>
        <span className="px-1.5 py-0.5 text-[10px] font-medium rounded shrink-0 bg-[var(--color-badge-md-bg)] text-[var(--color-badge-md-text)]">
          {format.toUpperCase()}
        </span>
      </div>

      {/* 内容摘要（2 行省略） */}
      <p className="text-xs text-[var(--color-text-secondary)] line-clamp-2 mb-3 leading-relaxed">
        {noteExcerpt(note.content)}
      </p>

      {/* 标签区：浅青绿圆角小标签（前 3 个） */}
      {note.tags && note.tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-3">
          {note.tags.slice(0, 3).map((tag) => (
            <span
              key={tag}
              className="px-2 py-0.5 text-[10px] rounded-full bg-[var(--color-tag-bg)] text-[var(--color-tag-text)]"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* 底部：分类名 + 发布日期 */}
      <div className="text-[10px] text-[var(--color-text-tertiary)]">
        {note.category || t('note.uncategorized')} · {formatDate(note.created_at)}
      </div>
    </div>
  );
}

export default memo(NoteCardItem);
