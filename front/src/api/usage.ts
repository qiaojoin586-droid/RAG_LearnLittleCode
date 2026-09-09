/**
 * 用量与费用 API
 *
 * 对应后端路由：/api/v1/usage/*
 */

import client from './client';
import { endpoints } from './endpoints';
import type { ApiResponse, UsageSummary } from '../types/api';

export const usageApi = {
  /** 模型调用用量与费用汇总（days：1-365，默认 30） */
  getSummary: (days = 30) =>
    client.get<ApiResponse<UsageSummary>>(endpoints.usage.summary, {
      params: { days },
    }),
};
