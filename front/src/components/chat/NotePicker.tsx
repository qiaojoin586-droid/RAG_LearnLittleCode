/**
 * 引用笔记选择面板（AIChat 输入区）
 *
 * 职责：懒加载笔记列表、搜索过滤、多选切换、点击外部关闭
 *
 * 审查 ⑤：从 AIChat.tsx（1088 行巨型组件）拆出。
 */

import { useEffect, useRef, useState } from 'react';
import { FileText, Search, X } from 'lucide-react';
import { notesApi } from '../../api/notes';
import { useFlipAlignment } from '../../hooks/useFlipAlignment';
import type { Note } from '../../types/api';

interface Props {
  open: boolean;
  onClose: () => void;
  /** 已选中笔记 ID 集合（多选） */
  selectedIds: Set<string>;
  onToggle: (note: Note) => void;
}

export default function NotePicker({ open, onClose, selectedIds, onToggle }: Props) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // 视口边界检测：右边界超视口时翻转对齐（AI 浮层贴着屏幕右缘，左对齐会出屏）
  const alignRight = useFlipAlignment(open, panelRef);

  // 打开时懒加载笔记列表
  useEffect(() => {
    if (!open) return;
    setSearch('');
    setLoading(true);
    notesApi
      .list({ page: 1, page_size: 100 })
      .then((res) => setNotes(res.data?.data?.notes ?? []))
      .catch((err) => console.error('[NotePicker] 加载笔记列表失败:', err))
      .finally(() => setLoading(false));
  }, [open]);

  // 点击面板外部关闭
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open, onClose]);

  if (!open) return null;

  const filtered = notes.filter((n) =>
    n.title.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div
      ref={panelRef}
      className={`absolute bottom-full mb-2 w-72 max-w-[80vw] max-h-64 overflow-hidden rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-card)] shadow-lg z-10 flex flex-col ${
        alignRight ? 'right-0' : 'left-0'
      }`}
    >
      {/* 搜索栏 */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[var(--color-border)]">
        <Search size={14} className="text-[var(--color-text-tertiary)]" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索笔记..."
          className="flex-1 text-sm bg-transparent text-[var(--color-text)] placeholder:text-[var(--color-text-tertiary)] focus:outline-none"
        />
        <button onClick={onClose} className="text-[var(--color-text-tertiary)] hover:text-[var(--color-text)]">
          <X size={14} />
        </button>
      </div>
      {/* 笔记列表 */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="text-center py-6 text-sm text-[var(--color-text-tertiary)]">加载中...</div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-6 text-sm text-[var(--color-text-tertiary)]">暂无笔记</div>
        ) : (
          filtered.map((note) => {
            const isSelected = selectedIds.has(note.id);
            return (
              <button
                key={note.id}
                onClick={() => onToggle(note)}
                className={`w-full text-left px-3 py-2 text-sm flex items-center gap-2 transition-colors ${
                  isSelected
                    ? 'bg-[var(--color-accent-bg)] text-[var(--color-accent)]'
                    : 'text-[var(--color-text)] hover:bg-[var(--color-accent-bg)]'
                }`}
              >
                <FileText size={14} className="flex-shrink-0" />
                <span className="truncate">{note.title}</span>
                {note.category && (
                  <span className="ml-auto text-xs text-[var(--color-text-tertiary)] flex-shrink-0">{note.category}</span>
                )}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
