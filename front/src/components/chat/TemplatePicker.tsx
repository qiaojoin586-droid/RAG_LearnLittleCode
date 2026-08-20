/**
 * PPT 模板选择面板（AIChat 输入区，v1.4）
 *
 * 职责：懒加载模板列表、单选切换、点击外部关闭
 *
 * 审查 ⑤：从 AIChat.tsx（1088 行巨型组件）拆出。
 */

import { useEffect, useRef, useState } from 'react';
import { Presentation, X } from 'lucide-react';
import { pptTemplatesApi, type PptTemplateInfo } from '../../api/pptTemplates';
import { useFlipAlignment } from '../../hooks/useFlipAlignment';

interface Props {
  open: boolean;
  onClose: () => void;
  /** 当前选中的模板（单选；null 表示未选中） */
  selected: PptTemplateInfo | null;
  onSelect: (tmpl: PptTemplateInfo) => void;
}

export default function TemplatePicker({ open, onClose, selected, onSelect }: Props) {
  const [templates, setTemplates] = useState<PptTemplateInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // 视口边界检测：右边界超视口时翻转对齐（AI 浮层贴着屏幕右缘，左对齐会出屏）
  const alignRight = useFlipAlignment(open, panelRef);

  // 打开时懒加载模板列表
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    pptTemplatesApi
      .list()
      .then((res) => setTemplates(res.data?.data?.templates ?? []))
      .catch(() => setTemplates([]))
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

  return (
    <div
      ref={panelRef}
      className={`absolute bottom-full mb-2 w-72 max-w-[80vw] max-h-64 overflow-hidden rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-card)] shadow-lg z-10 flex flex-col ${
        alignRight ? 'right-0' : 'left-0'
      }`}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[var(--color-border)]">
        <Presentation size={14} className="text-[var(--color-text-tertiary)]" />
        <span className="flex-1 text-sm text-[var(--color-text)]">选择 PPT 模板</span>
        <button
          onClick={onClose}
          className="text-[var(--color-text-tertiary)] hover:text-[var(--color-text)]"
        >
          <X size={14} />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="text-center py-6 text-sm text-[var(--color-text-tertiary)]">加载中...</div>
        ) : templates.length === 0 ? (
          <div className="text-center py-6 text-sm text-[var(--color-text-tertiary)]">
            暂无模板，请先在「PPT 模板」页上传
          </div>
        ) : (
          templates.map((tmpl) => {
            const isSelected = selected?.id === tmpl.id;
            return (
              <button
                key={tmpl.id}
                onClick={() => onSelect(tmpl)}
                className={`w-full text-left px-3 py-2 text-sm flex items-center gap-2 transition-colors ${
                  isSelected
                    ? 'bg-[var(--color-accent-bg)] text-[var(--color-accent)]'
                    : 'text-[var(--color-text)] hover:bg-[var(--color-accent-bg)]'
                }`}
              >
                <Presentation size={14} className="flex-shrink-0" />
                <span className="truncate">{tmpl.name}</span>
                {isSelected && <span className="ml-auto text-xs flex-shrink-0">✓</span>}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
