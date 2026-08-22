/**
 * 分类删除确认弹窗（展示影响范围，用户确认后软删除）
 *
 * 影响范围数据来源（设计 §4.3）：前端从分类树本地计算——
 * Category.note_count（含子孙）+ children 递归计数，无需额外预览端点。
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useCategoryStore, extractError } from '../../stores/useCategoryStore';
import type { Category } from '../../types/category';

interface CategoryDeleteDialogProps {
  open: boolean;
  category: Category;
  onClose: () => void;
}

/** 递归计算子孙分类数量 */
function countDescendants(children: Category[]): number {
  let count = 0;
  for (const c of children) {
    count += 1 + countDescendants(c.children ?? []);
  }
  return count;
}

export default function CategoryDeleteDialog({ open, category, onClose }: CategoryDeleteDialogProps) {
  const { t } = useTranslation();
  const { deleteCategory } = useCategoryStore();
  const [busy, setBusy] = useState(false);

  if (!open) return null;

  const subCount = countDescendants(category.children ?? []);

  const handleConfirm = async () => {
    setBusy(true);
    try {
      await deleteCategory(category.id);
      toast.success(t('note.categoryDeleted'));
      onClose();
    } catch (err) {
      toast.error(extractError(err) || t('common.error'));
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
        className="w-full max-w-md p-6 rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] shadow-[var(--shadow-dialog)]"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-bold text-[var(--color-text)]">
          {t('note.deleteConfirmTitle')}
        </h3>

        <p className="mt-3 text-sm text-[var(--color-text-secondary)] leading-relaxed">
          {t('note.deleteConfirmMsg', {
            name: category.name,
            subCount,
            noteCount: category.note_count,
          })}
        </p>
        <ul className="mt-3 space-y-1 text-sm text-[var(--color-text-tertiary)]">
          {subCount > 0 && <li>· {t('note.deleteConfirmSubTip')}</li>}
          <li>· {t('note.deleteConfirmNoteTip', { count: category.note_count })}</li>
        </ul>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm rounded-xl border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:text-[var(--color-text)] transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleConfirm}
            disabled={busy}
            className="px-4 py-2 text-sm rounded-full bg-[var(--color-danger)] text-white hover:opacity-90 disabled:opacity-50 transition-all"
          >
            {t('common.delete')}
          </button>
        </div>
      </div>
    </div>
  );
}
