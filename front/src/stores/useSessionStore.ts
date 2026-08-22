/**
 * 聊天会话状态管理
 * 
 * 职责：管理当前对话上下文（会话列表、当前会话 ID）。
 * 不持久化：页面刷新后清空，重新从后端加载。
 */

import { create } from 'zustand';
import type { ChatSession, ChatMessage } from '../types/api';

interface SessionState {
  /** 会话列表 */
  sessions: ChatSession[];
  /** 当前活跃会话 ID */
  currentSessionId: string | null;
  /** 最近一次有交互的会话 ID（用于路由恢复） */
  lastSessionId: string | null;
  /** 当前会话的消息列表 */
  messages: ChatMessage[];
  /** 是否正在加载消息 */
  isLoadingMessages: boolean;

  /** 设置会话列表（支持函数式更新，v2：浮层历史视图增删改用） */
  setSessions: (sessions: ChatSession[] | ((prev: ChatSession[]) => ChatSession[])) => void;
  /** 设置当前会话 */
  setCurrentSession: (sessionId: string | null) => void;
  /** 记录最近会话 ID */
  setLastSessionId: (sessionId: string | null) => void;
  /** 追加一条消息 */
  addMessage: (message: ChatMessage) => void;
  /** 设置消息列表（支持函数式更新，v1.6：tool_file 挂载用） */
  setMessages: (messages: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => void;
  /** 更新最后一条 AI 消息内容（SSE 流式追加） */
  updateLastAssistantMessage: (content: string) => void;
  /** 设置加载状态 */
  setLoadingMessages: (loading: boolean) => void;
  /** 清空当前会话状态（保留 lastSessionId） */
  clearCurrentSession: () => void;
  /** 彻底重置所有会话状态（登出时调用） */
  resetAll: () => void;
}

export const useSessionStore = create<SessionState>()((set) => ({
  sessions: [],
  currentSessionId: null,
  lastSessionId: null,
  messages: [],
  isLoadingMessages: false,

  setSessions: (sessions) =>
    set((state) => ({
      sessions:
        typeof sessions === 'function'
          ? (sessions as (prev: ChatSession[]) => ChatSession[])(state.sessions)
          : sessions,
    })),

  setCurrentSession: (sessionId) => set({ currentSessionId: sessionId }),

  setLastSessionId: (sessionId) => set({ lastSessionId: sessionId }),

  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),

  setMessages: (messages) =>
    set((state) => ({
      messages:
        typeof messages === 'function'
          ? (messages as (prev: ChatMessage[]) => ChatMessage[])(state.messages)
          : messages,
    })),

  updateLastAssistantMessage: (content) =>
    set((state) => {
      const msgs = [...state.messages];
      const lastIdx = msgs.findLastIndex((m) => m.role === 'assistant');
      if (lastIdx >= 0) {
        msgs[lastIdx] = { ...msgs[lastIdx], content };
      }
      return { messages: msgs };
    }),

  setLoadingMessages: (loading) => set({ isLoadingMessages: loading }),

  clearCurrentSession: () =>
    set({ currentSessionId: null, lastSessionId: null, messages: [] }),

  resetAll: () =>
    set({ sessions: [], currentSessionId: null, lastSessionId: null, messages: [], isLoadingMessages: false }),
}));
