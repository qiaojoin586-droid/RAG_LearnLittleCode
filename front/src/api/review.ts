/**
 * 回顾 API
 * 
 * 对应后端路由：/api/v1/review/*
 */

import client from './client';
import { endpoints } from './endpoints';
import type {
  ApiResponse, ReviewRecord, ReviewStats, ReviewQuestionsResponse,
} from '../types/api';

export const reviewApi = {
  /** 获取今日待回顾列表 */
  getToday: () =>
    client.get<ApiResponse<{ reviews: ReviewRecord[]; count: number }>>(endpoints.review.today),

  /** 标记回顾完成 */
  complete: (reviewId: number, quality: number = 3) =>
    client.post<ApiResponse<{
      review_id: number;
      next_review_at: string;
      interval_days: number;
    }>>(`${endpoints.review.complete(reviewId)}?quality=${quality}`),

  /** 获取回顾统计 */
  getStats: () =>
    client.get<ApiResponse<ReviewStats>>(endpoints.review.stats),

  /** AI 生成复习题（LLM，主模型推理较慢，超时放宽到 60s） */
  generateQuestions: (reviewId: number) =>
    client.post<ApiResponse<ReviewQuestionsResponse>>(
      endpoints.review.questions(reviewId),
      null,
      { timeout: 60000 },
    ),
};
