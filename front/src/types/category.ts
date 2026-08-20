/**
 * 分类相关类型定义
 *
 * 对应后端 app/schemas/category.py（见 docs/笔记分类管理功能设计方案.md v4 §4.6）。
 */

/** "未分类"过滤哨兵值（与后端 note_service.UNCATEGORIZED_SENTINEL 保持一致） */
export const UNCATEGORIZED_SENTINEL = '__uncategorized__';

/** 分类树节点响应 */
export interface Category {
  id: string;
  parent_id: string | null;
  name: string;
  icon?: string | null;
  color?: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
  children: Category[];
  /** 含子孙分类下的笔记总数，排除软删除笔记 */
  note_count: number;
}

/** 分类树响应 */
export interface CategoryTreeResponse {
  categories: Category[];
  /** category_id IS NULL 的笔记数（排除软删除） */
  uncategorized_count: number;
}

/** 创建分类请求 */
export interface CategoryCreate {
  name: string;
  parent_id?: string | null;
  icon?: string;
  color?: string;
}

/** 更新分类请求（改父级走 move，排序走 reorder；icon/color 传 null 表示清除） */
export interface CategoryUpdate {
  name?: string;
  icon?: string | null;
  color?: string | null;
}

/** 移动分类请求（改父级） */
export interface CategoryMove {
  parent_id?: string | null;
}

/** 批量重排同级分类顺序请求 */
export interface CategoryReorder {
  parent_id?: string | null;
  ordered_ids: string[];
}

/** 分类批量操作请求 */
export interface CategoryBatchOperation {
  category_ids: string[];
  operation: 'delete' | 'merge';
  merge_target_id?: string;
}

/** 软删除影响范围（删除确认弹窗数据） */
export interface DeleteImpact {
  subcategory_count: number;
  note_count: number;
}

/** 回收站分类响应 */
export interface DeletedCategory {
  id: string;
  name: string;
  icon?: string | null;
  color?: string | null;
  deleted_at: string;
  days_remaining: number;
  /** 一并软删除的子孙分类数量 */
  descendant_count: number;
}

/** 回收站分类列表响应 */
export interface DeletedCategoryListResponse {
  categories: DeletedCategory[];
  total: number;
}

/** 将分类树扁平化为有序列表（父在前子在后，带深度），供下拉选择器使用 */
export function flattenCategories(
  categories: Category[],
  depth = 0
): Array<{ category: Category; depth: number }> {
  const result: Array<{ category: Category; depth: number }> = [];
  for (const cat of categories) {
    result.push({ category: cat, depth });
    if (cat.children?.length) {
      result.push(...flattenCategories(cat.children, depth + 1));
    }
  }
  return result;
}

/** 在分类树中查找指定 ID 的节点 */
export function findCategory(categories: Category[], id: string): Category | null {
  for (const cat of categories) {
    if (cat.id === id) return cat;
    const found = findCategory(cat.children ?? [], id);
    if (found) return found;
  }
  return null;
}

/** 判断 descendantId 是否为 ancestorId 的子孙（客户端预校验，服务端为准） */
export function isDescendant(
  categories: Category[],
  ancestorId: string,
  descendantId: string
): boolean {
  const ancestor = findCategory(categories, ancestorId);
  return !!ancestor && !!findCategory(ancestor.children ?? [], descendantId);
}
