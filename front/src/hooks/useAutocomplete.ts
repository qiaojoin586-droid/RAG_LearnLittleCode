/**
 * AI 联机补全 Hook（薄片版）
 *
 * 设计文档：docs/AI联机补全功能设计优化方案.md §4.2
 *
 * 薄片版与文档设计的差异：
 * - 只做「防抖 → 请求 → 状态管理」，UI 由调用方渲染（暂不做内联 ghost text 覆盖层）
 * - 新增最小请求间隔：后端限流 10 次/分钟（§3.4），默认 7s ≈ 最多 8~9 次/分钟，
 *   避免打满配额后静默降级（用户感知"补全时有时无"）
 *
 * 行为：
 * - 内容变化 → 600ms 防抖后请求；新请求前 abort 上一次
 * - 有选区 / 光标前 < 10 字符 / 距上次请求过近 → 不请求
 * - accept() 返回 {text, cursorPosition}，cursorPosition 为完整笔记内容中的插入点
 * - 组件卸载时清理定时器与在途请求
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';

import { notesApi } from '../api/notes';

interface UseAutocompleteOptions {
  /** 停顿延迟（ms），默认 600 */
  debounceMs?: number;
  /** 是否启用，默认 true */
  enabled?: boolean;
  /** 两次请求最小间隔（ms），默认 7000（对齐后端 10 次/分钟限流） */
  minIntervalMs?: number;
}

export interface AutocompleteAcceptResult {
  text: string;
  /** 完整笔记内容中的插入位置（Tab 采纳用） */
  cursorPosition: number;
}

export function useAutocomplete(options?: UseAutocompleteOptions) {
  const { debounceMs = 600, enabled = true, minIntervalMs = 7000 } = options ?? {};

  const [completion, setCompletion] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  /** 建议对应的完整内容光标偏移（采纳时的插入点） */
  const [anchor, setAnchor] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastRequestAtRef = useRef(0);

  const reset = useCallback(() => {
    setCompletion('');
    setIsLoading(false);
    setAnchor(0);
  }, []);

  /** 丢弃建议并取消在途请求（Esc / 继续输入 / 光标移动 / 点击） */
  const dismiss = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (abortRef.current) abortRef.current.abort();
    abortRef.current = null;
    reset();
  }, [reset]);

  /**
   * 内容变化时调用（由编辑器的 onChange 触发）
   *
   * @param content 截断后的上下文（前 1200 + 后 300，API payload）
   * @param cursorPosition 截断内容内的光标偏移（API payload）
   * @param fullCursorPosition 完整内容内的光标偏移（采纳插入点）
   * @param title 笔记标题（上下文）
   */
  const onContentChange = useCallback((
    content: string,
    cursorPosition: number,
    fullCursorPosition: number,
    title: string,
  ) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (abortRef.current) abortRef.current.abort();
    abortRef.current = null;
    reset();

    // 触发条件（§2.1）：光标前 ≥ 10 字符
    if (!enabled || fullCursorPosition < 10) return;

    // 最小请求间隔（§3.4）：避免打满 10 次/分钟配额
    if (Date.now() - lastRequestAtRef.current < minIntervalMs) return;

    timerRef.current = setTimeout(async () => {
      const controller = new AbortController();
      abortRef.current = controller;
      lastRequestAtRef.current = Date.now();
      // ★ 发起请求即记录插入点：loading 的 "…" 与最终建议都落在光标处
      setAnchor(fullCursorPosition);
      setIsLoading(true);

      try {
        const res = await notesApi.autocomplete(
          { content, cursor_position: cursorPosition, title },
          { signal: controller.signal },
        );
        const data = res.data.data;
        if (data.completion) {
          setCompletion(data.completion);
          setAnchor(fullCursorPosition);
        } else {
          reset();
        }
      } catch (err) {
        // 主动取消不视为错误；其它错误静默降级（§5.1）
        if (!axios.isCancel(err)) reset();
      } finally {
        // 仅当仍是当前请求时才清 loading（避免被更新的请求覆盖）
        if (abortRef.current === controller) setIsLoading(false);
      }
    }, debounceMs);
  }, [debounceMs, enabled, minIntervalMs, reset]);

  /** 采纳建议（Tab）：返回文本与完整内容插入点 */
  const accept = useCallback((): AutocompleteAcceptResult => {
    const result = { text: completion, cursorPosition: anchor };
    reset();
    return result;
  }, [completion, anchor, reset]);

  // 卸载清理
  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (abortRef.current) abortRef.current.abort();
  }, []);

  return { completion, isLoading, anchor, onContentChange, accept, dismiss };
}
