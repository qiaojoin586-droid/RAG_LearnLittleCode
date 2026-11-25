/**
 * 知识库 API
 * 
 * 对应后端路由：/api/v1/knowledge/*
 * 
 * 上传接口返回 SSE 流式进度，使用原生 fetch。
 */

import client from './client';
import { endpoints } from './endpoints';
import type { ApiResponse, KnowledgeDocument, KnowledgeDocumentListResponse, KnowledgeUploadJob, KnowledgeUploadJobList } from '../types/api';

export const knowledgeApi = {
  /**
   * 上传知识库文档（SSE 流式进度推送）
   * 
   * 使用原生 fetch 发送 FormData。响应提供任务编号和 SSE 事件流。
   * 
   * @param file - 要上传的文件
   * @param signal - AbortSignal 用于取消
   * @returns fetch Response 对象（text/event-stream）
   */
  upload: async (file: File, signal?: AbortSignal): Promise<Response> => {
    const token = localStorage.getItem('jwt_token');
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch(endpoints.knowledge.upload, {
      method: 'POST',
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: formData,
      signal,
    });

    if (!response.ok) {
      throw new Error(`上传失败: ${response.status}`);
    }

    return response;
  },

  getUploadJob: (jobId: string, signal?: AbortSignal) =>
    client.get<ApiResponse<KnowledgeUploadJob>>(endpoints.knowledge.uploadJob(jobId), { signal }),

  listUploadJobs: (after: string | undefined, signal: AbortSignal) =>
    client.get<ApiResponse<KnowledgeUploadJobList>>(endpoints.knowledge.uploadJobs, { params: { after }, signal }),

  /** 获取文档列表 */
  listDocuments: (signal?: AbortSignal) =>
    client.get<ApiResponse<KnowledgeDocumentListResponse>>(endpoints.knowledge.documents, { signal }),

  /** 获取文档详情 */
  getDocument: (docId: number) =>
    client.get<ApiResponse<KnowledgeDocument>>(endpoints.knowledge.documentDetail(docId)),

  /** 删除文档 */
  deleteDocument: (docId: number) =>
    client.delete<ApiResponse<null>>(endpoints.knowledge.documentDetail(docId)),
};
