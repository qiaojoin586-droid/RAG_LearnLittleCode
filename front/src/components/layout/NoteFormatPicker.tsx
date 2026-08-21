/**
 * 新建笔记类型选择器（文档类型功能，2026-08-21）
 *
 * 点击「+」弹出类型下拉（Markdown 默认 / 纯文本），选中后跳转
 * /notes/new?format=md|txt，编辑器按参数进入对应模式。
 * 类型仅在新建时可选（已创建笔记不可改类型）。
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus, FileText, AlignLeft } from 'lucide-react';
import { NOTE_FORMATS } from '../../constants/noteFormats';

export default function NoteFormatPicker() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  return (
    <div className="relative" ref={menuRef}>
      {/* 新建按钮（与原「+」按钮视觉一致） */}
      <button
        onClick={() => setOpen((v) => !v)}
        className={`p-1.5 rounded-md text-white transition-colors ${
          open
            ? 'bg-[var(--color-accent-hover)]'
            : 'bg-[var(--color-accent)] hover:bg-[var(--color-accent-hover)]'
        }`}
        title={t('note.create')}
      >
        <Plus size={15} />
      </button>

      {/* 类型选择下拉 */}
      {open && (
        <div
          className="absolute right-0 top-9 w-44 py-1.5 rounded-[var(--radius-md)]
            bg-[var(--color-card)] border border-[var(--color-border)]
            shadow-[var(--shadow-dialog)] animate-fade-in"
          style={{ zIndex: 60 }}
        >
          <p className="px-3 py-1 text-[10px] text-[var(--color-text-tertiary)]">
            {t('note.format.pickerTitle')}
          </p>
          {NOTE_FORMATS.map((f) => {
            const Icon = f.value === 'md' ? FileText : AlignLeft;
            return (
              <button
                key={f.value}
                onClick={() => {
                  setOpen(false);
                  navigate(`/notes/new?format=${f.value}`);
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-[var(--color-text-secondary)]
                  hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-accent)] transition-colors duration-150"
              >
                <Icon size={15} />
                {t(f.labelKey)}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
