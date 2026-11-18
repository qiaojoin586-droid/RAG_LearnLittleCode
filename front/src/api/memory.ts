import client from './client';
import type { ApiResponse } from '../types/api';

export interface MemoryDocument {
  exists: boolean; filename: 'memory.md'; revision_id: string | null; revision_number: number;
  head_version: number; memory_epoch: number; content_md: string | null; content_hash: string | null;
  settings: { enabled: boolean; auto_extract: boolean; management_mode: 'auto' | 'manual' };
  extraction_available: boolean; updated_at: string | null;
}
export interface MemoryRevision { id: string; revision_number: number; source_kind: string; content_hash: string; created_at: string; content_md?: string }
export interface MemoryJob { id: string; status: string; generation_number: number; source_message_id: number;
  proposed_content_md: string | null; error_reason: string | null; applied_revision_id: string | null; created_at: string }
interface Page<T> { items: T[]; next_cursor?: string | null }
const base = '/api/v1/user/memory';
const data = <T>(response: { data: ApiResponse<T> }) => response.data.data;
const headers = (document: MemoryDocument, key: string) => ({ 'Idempotency-Key': key, 'If-Match': `"memory-${document.head_version}"` });
export const memoryApi = {
  get: (signal?: AbortSignal) => client.get<ApiResponse<MemoryDocument>>(base, { signal }).then(data),
  save: (document: MemoryDocument, content: string, key: string) => client.put<ApiResponse<MemoryDocument>>(base,
    { content_md: content }, { headers: headers(document, key) }).then(data),
  settings: (document: MemoryDocument, settings: Partial<MemoryDocument['settings']>, key: string) => client.patch<ApiResponse<MemoryDocument>>(
    `${base}/settings`, settings, { headers: headers(document, key) }).then(data),
  revisions: (cursor?: string, signal?: AbortSignal) => client.get<ApiResponse<Page<MemoryRevision>>>(`${base}/revisions`, { params: { cursor }, signal }).then(data),
  revision: (id: string, signal?: AbortSignal) => client.get<ApiResponse<MemoryRevision>>(`${base}/revisions/${id}`, { signal }).then(data),
  restore: (document: MemoryDocument, id: string, key: string) => client.post<ApiResponse<MemoryDocument>>(`${base}/revisions/${id}/restore`, {}, { headers: headers(document, key) }).then(data),
  clear: (document: MemoryDocument, purge: boolean, key: string) => client.post<ApiResponse<MemoryDocument>>(`${base}/clear`, { purge_history: purge }, { headers: headers(document, key) }).then(data),
  jobs: (signal?: AbortSignal) => client.get<ApiResponse<Page<MemoryJob>>>(`${base}/jobs`, { signal }).then(data),
  generate: (document: MemoryDocument, key: string) => client.post<ApiResponse<MemoryJob>>(`${base}/generate`, {}, { headers: headers(document, key) }).then(data),
  apply: (document: MemoryDocument, id: string, key: string) => client.post<ApiResponse<MemoryDocument>>(`${base}/jobs/${id}/apply`, {}, { headers: headers(document, key) }).then(data),
  dismiss: (document: MemoryDocument, id: string, key: string) => client.post<ApiResponse<MemoryDocument>>(`${base}/jobs/${id}/dismiss`, {}, { headers: headers(document, key) }).then(data),
  download: () => client.get<Blob>(`${base}/download`, { responseType: 'blob' }).then(response => response.data),
};

export function memoryError(error: unknown): string {
  const body = (error as { response?: { data?: { code?: number; message?: string } } })?.response?.data;
  return ({ 40940: '文件已被更新。你的未保存内容已保留，请先核对服务器版本再保存。',
    40941: '建议已过期或基线已变化，请重新生成。', 40944: '个人记忆提取角色未启用，或已关闭自动提取。仍可手工编辑文件。',
    42241: '请先完成一轮允许使用记忆的 AI 对话，再请求提取。', 41340: '文件超过 32768 字节，请缩短后保存。',
    42240: '内容包含疑似凭据或不支持的控制字符，请检查后再保存。', 41040: '该历史结果已被彻底清理。',
    50340: '个人记忆存储暂不可用，请稍后重试；不会静默跳过启用的记忆。',
  } as Record<number, string>)[body?.code ?? 0] ?? body?.message ?? '请求未完成，请稍后重试。';
}
