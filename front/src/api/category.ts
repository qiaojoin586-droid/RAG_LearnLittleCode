/**
 * 分类相关 API
 *
 * 对应后端路由：/api/v1/category*
 */

import client from './client';
import { endpoints } from './endpoints';
import type { ApiResponse } from '../types/api';
import type {
  CategoryTreeResponse, CategoryCreate, CategoryUpdate, CategoryMove,
  CategoryReorder, CategoryBatchOperation, DeleteImpact,
  DeletedCategoryListResponse,
} from '../types/category';

export const categoryApi = {
  /** 获取分类树（含 note_count 与未分类统计） */
  getTree: () =>
    client.get<ApiResponse<CategoryTreeResponse>>(endpoints.category.tree),

  /** 创建分类 */
  create: (data: CategoryCreate) =>
    client.post<ApiResponse<unknown>>(endpoints.category.base, data),

  /** 更新分类（名称/图标/颜色） */
  update: (id: string, data: CategoryUpdate) =>
    client.put<ApiResponse<unknown>>(endpoints.category.detail(id), data),

  /** 软删除分类（返回影响范围） */
  delete: (id: string) =>
    client.delete<ApiResponse<DeleteImpact>>(endpoints.category.detail(id)),

  /** 从回收站恢复分类（级联恢复祖先链） */
  restore: (id: string) =>
    client.post<ApiResponse<unknown>>(endpoints.category.restore(id)),

  /** 移动分类（改父级；null = 移到顶级） */
  move: (id: string, data: CategoryMove) =>
    client.post<ApiResponse<unknown>>(endpoints.category.move(id), data),

  /** 批量重排同级分类顺序 */
  reorder: (data: CategoryReorder) =>
    client.post<ApiResponse<unknown>>(endpoints.category.reorder, data),

  /** 批量操作（删除/合并） */
  batch: (data: CategoryBatchOperation) =>
    client.post<ApiResponse<unknown>>(endpoints.category.batch, data),

  /** 回收站分类列表 */
  getRecycleBin: () =>
    client.get<ApiResponse<DeletedCategoryListResponse>>(endpoints.category.recycleBin),
};
