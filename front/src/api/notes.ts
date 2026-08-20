/**
 * 笔记相关 API
 * 
 * 对应后端路由：/api/v1/note*
 */

import client from './client';
import { endpoints } from './endpoints';
import type {
  ApiResponse, Note, NoteCreate, NoteUpdate, NoteListResponse,
  NoteSearchRequest, NoteSearchResult, BatchOperation, DeletedNoteListResponse,
} from '../types/api';

export const notesApi = {
  /** 创建笔记 */
  create: (data: NoteCreate) =>
    client.post<ApiResponse<Note>>(endpoints.note.base, data),

  /** 获取笔记列表（分页；category_id 优先，__uncategorized__ = 未分类） */
  list: (params?: { page?: number; page_size?: number; category?: string; category_id?: string }) =>
    client.get<ApiResponse<NoteListResponse>>(endpoints.note.base, { params }),

  /** 获取笔记详情 */
  detail: (id: string) =>
    client.get<ApiResponse<Note>>(endpoints.note.detail(id)),

  /** 更新笔记 */
  update: (id: string, data: NoteUpdate) =>
    client.put<ApiResponse<Note>>(endpoints.note.detail(id), data),

  /** 删除笔记（软删除 → 移入回收站） */
  delete: (id: string) =>
    client.delete<ApiResponse<null>>(endpoints.note.detail(id)),

  /** 回收站列表（分页） */
  listDeleted: (params?: { page?: number; page_size?: number }) =>
    client.get<ApiResponse<DeletedNoteListResponse>>(endpoints.note.recycleBin, { params }),

  /** 恢复笔记（从回收站还原） */
  restore: (id: string) =>
    client.post<ApiResponse<null>>(endpoints.note.restore(id)),

  /** 彻底删除笔记（不可恢复） */
  permanentDelete: (id: string) =>
    client.delete<ApiResponse<null>>(endpoints.note.permanent(id)),

  /** 移动笔记到分类（categoryId 为空 = 移为未分类） */
  moveToCategory: (id: string, categoryId: string | null) =>
    client.put<ApiResponse<Note>>(
      endpoints.note.moveCategory(id),
      null,
      { params: categoryId ? { category_id: categoryId } : {} }
    ),

  /** 语义搜索 */
  search: (data: NoteSearchRequest) =>
    client.post<ApiResponse<{ query: string; results: NoteSearchResult[] }>>(endpoints.note.search, data),

  /** 批量操作 */
  batch: (data: BatchOperation) =>
    client.post<ApiResponse<null>>(endpoints.note.batch, data),

  /** AI 内联补全 */
  autocomplete: (data: { content: string; cursor_position?: number }) =>
    client.post<ApiResponse<{ completion: string }>>(endpoints.note.autocomplete, data),

  /** AI 写作辅助 */
  writeAssistant: (data: { content: string; mode?: string }) =>
    client.post<ApiResponse<{ result: string }>>(endpoints.note.writeAssistant, data),
};
