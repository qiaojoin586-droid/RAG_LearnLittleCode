/**
 * SSE 流式通信 Hook
 *
 * 核心功能：
 * 1. 使用原生 fetch + ReadableStream 逐行解析 SSE 事件
 * 2. 支持聊天 SSE（thinking/response/done/error）和知识库 SSE（processing/completed/finish）
 * 3. AbortController 支持取消
 *
 * 解析逻辑已抽到 utils/sseParser.ts（SSEParser + dispatchSSEEvents），
 * 本 Hook 只负责 fetch 生命周期与回调接线。
 */

import { useRef, useCallback } from 'react';
import type { KnowledgeSSEMessage, ToolFileInfo } from '../types/api';
import { runChatStream } from '../utils/chatStream';
import type { SSEDispatchCallbacks } from '../utils/sseParser';

/** SSE 回调配置 */
interface SSECallbacks {
  /** 思考过程事件（RAG 管线各阶段） */
  onThinking?: (stage: string, content: string, details?: Record<string, unknown>) => void;
  /** 回答内容 chunk */
  onResponse?: (content: string, sessionId?: string, replace?: boolean) => void;
  /** 完成事件 */
  onDone?: (sessionId?: string) => void;
  onCitations?: SSEDispatchCallbacks['onCitations'];
  /** 错误事件 */
  onError?: (message: string) => void;
  /** 知识库进度事件 */
  onKnowledgeProgress?: (data: KnowledgeSSEMessage) => void;
  // Plan-and-Execute 事件回调
  onPlanStart?: (goal: string, totalSteps: number) => void;
  onPlanStep?: (step: number, action: string, status: string) => void;
  onPlanStepStart?: (step: number, action: string) => void;
  onPlanStepEnd?: (step: number, result: string) => void;
  onPlanSynthesize?: () => void;
  onPlanComplete?: (totalSteps: number, completedSteps: number) => void;
  onPlanFallback?: (reason: string) => void;
  /** 反思事件（L1 综合后自检：checking/refining） */
  onReflection?: (stage: string, round: number) => void;
  // 工具调用事件回调
  onToolStart?: (name: string) => void;
  onToolEnd?: (name: string, durationMs?: number) => void;
  /** 工具产出文件事件（PPT 生成完成，含下载信息，§6.3） */
  onToolFile?: (info: ToolFileInfo) => void;
}

export function useSSE() {
  const abortRef = useRef<AbortController | null>(null);

  /**
   * 启动 SSE 连接
   *
   * @param url - 请求 URL
   * @param body - 请求体（POST）
   * @param callbacks - 事件回调
   */
  const start = useCallback(async (
    url: string,
    body: Record<string, unknown>,
    callbacks: SSECallbacks,
  ) => {
    // 取消上一次连接
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const token = localStorage.getItem('jwt_token');

    try {
      await runChatStream(url, body, callbacks as SSEDispatchCallbacks, controller.signal, token);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, []);

  /**
   * 停止当前 SSE 连接
   */
  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  return { start, stop };
}
