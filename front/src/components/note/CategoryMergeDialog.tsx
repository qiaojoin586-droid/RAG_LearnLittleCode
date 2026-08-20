/**
 * 分类合并对话框：将源分类（含子分类与笔记）并入目标分类
 *
 * 目标候选：扁平化树，排除源分类自身及其子孙（服务端双重校验）。
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useCategoryStore, extractError } from '../../stores/useCategoryStore';
import { flattenCategories, isDescendant, type Category } from '../../types/category';

interface CategoryMergeDialogProps {
  open: boolean;
  source: Category;
  onClose: () => void;
}

export default function CategoryMergeDialog({ open, source, onClose }: CategoryMergeDialogProps) {
  const { t } = useTranslation();
  const { categories, mergeCategories } = useCategoryStore();
  const [targetId, setTargetId] = useState('');
  const [busy, setBusy] = useState(false);

  if (!open) return null;

  // 排除源分类自身及其子孙
  const options = flattenCategories(categories).filter(
    ({ category: c }) => c.id !== source.id && !isDescendant(categories, source.id, c.id)
  );

  const handleConfirm = async () => {
    if (!targetId) {
      toast.error(t('note.mergeTargetRequired'));
      return;
    }
    setBusy(true);
    try {
      await mergeCategories([source.id], targetId);
      toast.success(t('note.categoryMerged'));
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
        className="w-full max-w-sm p-6 rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] shadow-[var(--shadow-dialog)]"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-bold text-[var(--color-text)]">
          {t('note.mergeCategory')}
        </h3>
        <p className="mt-2 text-sm text-[var(--color-text-secondary)]">
          {t('note.mergeCategoryDesc', { name: source.name })}
        </p>

        <label className="block mt-4 text-xs text-[var(--color-text-tertiary)] mb-1">
          {t('note.mergeTarget')}
        </label>
        <select
          value={targetId}
          onChange={(e) => setTargetId(e.target.value)}
          className="w-full px-3 py-2 text-sm rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)] cursor-pointer"
        >
          <option value="">{t('note.mergeTargetPlaceholder')}</option>
          {options.map(({ category: c, depth }) => (
            <option key={c.id} value={c.id}>
              {' '.repeat(depth * 3) + c.name}
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
            {t('note.merge')}
          </button>
        </div>
      </div>
    </div>
  );
}
