/**
 * 分类状态管理（Zustand v5，与项目其他 store 一致）
 *
 * 数据源：GET /category/tree（后端动态分类树，替代原静态 noteCategoryTree.ts）
 * 所有写操作成功后统一重新拉取分类树，保证树与后端一致（个人体量，简单优先）。
 *
 * 选中语义：
 * - selectedCategoryId = null → 所有笔记（不过滤）
 * - selectedCategoryId = UNCATEGORIZED_SENTINEL → 未分类
 * - 其他 → 具体分类 ID
 */

import { create } from 'zustand';
import { categoryApi } from '../api/category';
import {
  UNCATEGORIZED_SENTINEL,
  type Category, type CategoryCreate, type CategoryUpdate,
  type CategoryReorder, type DeleteImpact,
} from '../types/category';

interface CategoryState {
  categories: Category[];
  /** 未分类笔记数（category_id IS NULL） */
  uncategorizedCount: number;
  selectedCategoryId: string | null;
  loading: boolean;
  /** 加载失败标记（非空时树区域展示失败提示与重试按钮） */
  error: string | null;

  fetchCategories: () => Promise<void>;
  createCategory: (data: CategoryCreate) => Promise<void>;
  updateCategory: (id: string, data: CategoryUpdate) => Promise<void>;
  deleteCategory: (id: string) => Promise<DeleteImpact | null>;
  restoreCategory: (id: string) => Promise<void>;
  moveCategory: (id: string, targetParentId: string | null) => Promise<void>;
  reorderCategories: (data: CategoryReorder) => Promise<void>;
  mergeCategories: (sourceIds: string[], targetId: string) => Promise<void>;
  selectCategory: (id: string | null) => void;
}

/** 提取后端错误消息（BusinessError.message） */
function extractError(err: unknown): string {
  const e = err as { response?: { data?: { message?: string } } };
  return e.response?.data?.message || '';
}

export const useCategoryStore = create<CategoryState>((set, get) => ({
  categories: [],
  uncategorizedCount: 0,
  selectedCategoryId: null,
  loading: false,
  error: null,

  fetchCategories: async () => {
    set({ loading: true });
    try {
      const res = await categoryApi.getTree();
      const data = res.data.data;
      set({
        categories: data.categories ?? [],
        uncategorizedCount: data.uncategorized_count ?? 0,
        error: null,
      });
    } catch {
      // 加载失败不再静默：置 error，树区域展示失败提示与重试按钮
      set({ error: 'categoryFetchFailed' });
    } finally {
      set({ loading: false });
    }
  },

  createCategory: async (data) => {
    await categoryApi.create(data);
    await get().fetchCategories();
  },

  updateCategory: async (id, data) => {
    await categoryApi.update(id, data);
    await get().fetchCategories();
  },

  deleteCategory: async (id) => {
    const res = await categoryApi.delete(id);
    await get().fetchCategories();
    return res.data.data ?? null;
  },

  restoreCategory: async (id) => {
    await categoryApi.restore(id);
    await get().fetchCategories();
  },

  moveCategory: async (id, targetParentId) => {
    await categoryApi.move(id, { parent_id: targetParentId });
    await get().fetchCategories();
  },

  reorderCategories: async (data) => {
    try {
      await categoryApi.reorder(data);
    } finally {
      // 无论成败都刷新（失败时避免"父级已改、顺序未定"的中间态）
      await get().fetchCategories();
    }
  },

  mergeCategories: async (sourceIds, targetId) => {
    await categoryApi.batch({ category_ids: sourceIds, operation: 'merge', merge_target_id: targetId });
    await get().fetchCategories();
  },

  selectCategory: (id) => set({ selectedCategoryId: id }),
}));

export { UNCATEGORIZED_SENTINEL, extractError };
