/**
 * 分类创建/编辑对话框
 *
 * 纯 React 实现，复用 ConfirmDialog 的 fixed 遮罩模式（在无 transform 的 main 内正常定位）。
 * 功能：名称 / 图标（emoji 预设）/ 颜色（预设色板）/ 父分类（仅创建时，改父级走拖拽 move）。
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useCategoryStore, extractError } from '../../stores/useCategoryStore';
import { flattenCategories, type Category } from '../../types/category';

interface CategoryDialogProps {
  open: boolean;
  /** 编辑时传入，创建时不传 */
  category?: Category;
  /** 创建子分类时传入父级 ID */
  parentId?: string;
  onClose: () => void;
}

/** emoji 预设（与图标字段约定一致） */
const EMOJI_OPTIONS = ['📁', '💼', '📚', '💻', '🏠', '✈️', '🍳', '🎨', '⚡', '🧠', '📝', '🎯'];
/** 颜色预设（与设计 §5.3 DEFAULT_COLORS 一致） */
const COLOR_OPTIONS = [
  '#4a9eff', '#52c41a', '#faad14', '#f5222d',
  '#722ed1', '#13c2c2', '#eb2f96', '#fa8c16',
];

export default function CategoryDialog({ open, category, parentId, onClose }: CategoryDialogProps) {
  const { t } = useTranslation();
  const { categories, createCategory, updateCategory } = useCategoryStore();

  const isEdit = !!category;
  const [name, setName] = useState('');
  const [icon, setIcon] = useState('');
  const [color, setColor] = useState('');
  const [selectedParentId, setSelectedParentId] = useState('');
  const [saving, setSaving] = useState(false);

  // 打开时重置表单（编辑回填，创建使用 parentId 默认值）
  useEffect(() => {
    if (open) {
      setName(category?.name ?? '');
      setIcon(category?.icon ?? '');
      setColor(category?.color ?? '');
      setSelectedParentId(isEdit ? (category?.parent_id ?? '') : (parentId ?? ''));
    }
  }, [open, category, parentId, isEdit]);

  if (!open) return null;

  /** 保存（创建或更新，错误消息来自后端 BusinessError） */
  const handleSave = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      toast.error(t('note.categoryNameRequired'));
      return;
    }
    setSaving(true);
    try {
      if (isEdit) {
        await updateCategory(category!.id, { name: trimmed, icon: icon || null, color: color || null });
        toast.success(t('note.categoryUpdated'));
      } else {
        await createCategory({
          name: trimmed,
          parent_id: selectedParentId || null,
          icon: icon || undefined,
          color: color || undefined,
        });
        toast.success(t('note.categoryCreated'));
      }
      onClose();
    } catch (err) {
      toast.error(extractError(err) || t('common.error'));
    } finally {
      setSaving(false);
    }
  };

  // 父分类候选（仅创建时）：扁平化树 + 深度缩进
  const parentOptions = flattenCategories(categories);

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
          {isEdit ? t('note.editCategory') : t('note.newCategory')}
        </h3>

        {/* 名称 */}
        <label className="block mt-4 text-xs text-[var(--color-text-tertiary)] mb-1">
          {t('note.categoryName')}
        </label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={100}
          autoFocus
          onKeyDown={(e) => e.key === 'Enter' && handleSave()}
          placeholder={t('note.categoryName')}
          className="w-full px-3 py-2 text-sm rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)] transition-all"
        />

        {/* 父分类（仅创建） */}
        {!isEdit && (
          <>
            <label className="block mt-4 text-xs text-[var(--color-text-tertiary)] mb-1">
              {t('note.parentCategory')}
            </label>
            <select
              value={selectedParentId}
              onChange={(e) => setSelectedParentId(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)] cursor-pointer"
            >
              <option value="">{t('note.topLevel')}</option>
              {parentOptions.map(({ category: c, depth }) => (
                <option key={c.id} value={c.id}>
                  {' '.repeat(depth * 3) + c.name}
                </option>
              ))}
            </select>
          </>
        )}

        {/* 图标 */}
        <label className="block mt-4 text-xs text-[var(--color-text-tertiary)] mb-1">
          {t('note.categoryIcon')}
        </label>
        <div className="flex flex-wrap gap-1.5">
          {EMOJI_OPTIONS.map((emoji) => (
            <button
              key={emoji}
              onClick={() => setIcon(icon === emoji ? '' : emoji)}
              className={`w-8 h-8 flex items-center justify-center text-base rounded-lg border transition-all ${
                icon === emoji
                  ? 'border-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                  : 'border-[var(--color-border)] hover:border-[var(--color-accent)]'
              }`}
            >
              {emoji}
            </button>
          ))}
        </div>

        {/* 颜色 */}
        <label className="block mt-4 text-xs text-[var(--color-text-tertiary)] mb-1">
          {t('note.categoryColor')}
        </label>
        <div className="flex flex-wrap gap-2">
          {COLOR_OPTIONS.map((c) => (
            <button
              key={c}
              onClick={() => setColor(color === c ? '' : c)}
              className={`w-6 h-6 rounded-full transition-all ${
                color === c ? 'ring-2 ring-offset-2 ring-[var(--color-accent)]' : ''
              }`}
              style={{ backgroundColor: c }}
            />
          ))}
        </div>

        {/* 操作 */}
        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm rounded-xl border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:text-[var(--color-text)] transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !name.trim()}
            className="px-4 py-2 text-sm rounded-full bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] disabled:opacity-50 transition-all"
          >
            {t('note.save')}
          </button>
        </div>
      </div>
    </div>
  );
}
