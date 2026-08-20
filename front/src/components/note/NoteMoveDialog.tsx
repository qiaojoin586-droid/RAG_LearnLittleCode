/**
 * 移动笔记到分类对话框（批量模式下使用，设计 §4.2/阶段三）
 *
 * 通过 POST /note/batch（operation=move + target_category_id）批量移动，
 * 后端双写 category 字符串。目标选择为扁平化分类树下拉。
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { notesApi } from '../../api/notes';
import { useCategoryStore } from '../../stores/useCategoryStore';
import { flattenCategories } from '../../types/category';

interface NoteMoveDialogProps {
  open: boolean;
  noteIds: string[];
  onClose: () => void;
  /** 移动成功后回调（刷新列表） */
  onMoved: () => void;
}

export default function NoteMoveDialog({ open, noteIds, onClose, onMoved }: NoteMoveDialogProps) {
  const { t } = useTranslation();
  const categories = useCategoryStore((s) => s.categories);
  const [targetId, setTargetId] = useState('');
  const [busy, setBusy] = useState(false);

  if (!open) return null;

  const handleConfirm = async () => {
    if (!targetId) {
      toast.error(t('note.moveTargetRequired'));
      return;
    }
    setBusy(true);
    try {
      const res = await notesApi.batch({
        note_ids: noteIds,
        operation: 'move',
        target_category_id: targetId,
      });
      const data = res.data.data as unknown as { success_count: number };
      toast.success(t('note.moveDone', { count: data.success_count ?? noteIds.length }));
      onMoved();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || t('common.error'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[2147483647] flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm p-6 rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] shadow-[var(--shadow-dialog)]"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-bold text-[var(--color-text)]">{t('note.moveToCategory')}</h3>
        <p className="mt-2 text-sm text-[var(--color-text-secondary)]">
          {t('note.moveCount', { count: noteIds.length })}
        </p>

        <label className="block mt-4 text-xs text-[var(--color-text-tertiary)] mb-1">
          {t('note.targetCategory')}
        </label>
        <select
          value={targetId}
          onChange={(e) => setTargetId(e.target.value)}
          className="w-full px-3 py-2 text-sm rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)] cursor-pointer"
        >
          <option value="">{t('note.targetCategoryPlaceholder')}</option>
          {flattenCategories(categories).map(({ category: c, depth }) => (
            <option key={c.id} value={c.id}>
              {' '.repeat(depth * 3) + (c.icon ? `${c.icon} ` : '') + c.name}
            </option>
          ))}
        </select>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm rounded-xl border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:text-[var(--color-text)] transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleConfirm}
            disabled={busy || !targetId}
            className="px-4 py-2 text-sm rounded-full bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] disabled:opacity-50 transition-all"
          >
            {t('note.move')}
          </button>
        </div>
      </div>
    </div>
  );
}
