/**
 * 当前浏览笔记上下文（v2：AI 对话面板自动关联）
 *
 * 桥接笔记详情区与 AI 浮层：
 * - NoteList 详情区打开/切换笔记时写入 currentNote；离开笔记页（卸载）时清空
 * - useChatComposer 读取作为「自动关联」的默认上下文：仅在用户未手动选择
 *   任何笔记时生效，手动选择优先（需求边界 2）
 *
 * 非笔记详情页（知识库/回顾等）不会写入，浮层保持原有逻辑。
 */

import { create } from 'zustand';
import type { Note } from '../types/api';

interface NoteContextState {
  /** 当前详情区打开的笔记（仅笔记详情页写入；其他页面为 null） */
  currentNote: Note | null;
  setCurrentNote: (note: Note | null) => void;
}

export const useNoteContextStore = create<NoteContextState>()((set) => ({
  currentNote: null,
  setCurrentNote: (note) => set({ currentNote: note }),
}));
