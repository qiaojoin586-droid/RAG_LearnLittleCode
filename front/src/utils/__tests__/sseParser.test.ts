/**
 * SSE 解析器单元测试（审查 P1-2 前端测试网）
 *
 * 覆盖：
 * - SSEParser：分块边界拆分、不完整行保留、非 data 行跳过、坏 JSON 跳过、flush
 * - dispatchSSEEvents：聊天 / Plan / 工具 / 知识库事件路由
 */

import { describe, it, expect, vi } from 'vitest';
import { SSEParser, dispatchSSEEvents } from '../sseParser';

describe('SSEParser', () => {
  it('解析单行 data 事件', () => {
    const p = new SSEParser();
    p.feed('data: {"type":"response","content":"hi"}\n');
    expect(p.takeEvents()).toEqual([{ type: 'response', content: 'hi' }]);
  });

  it('分块边界处切断的行跨 chunk 正确拼接', () => {
    const p = new SSEParser();
    // "data: {…}" 在 chunk 中间被切断
    p.feed('data: {"type":"respons');
    expect(p.takeEvents()).toEqual([]); // 不完整，未解析
    p.feed('e","content":"hi"}\n');
    expect(p.takeEvents()).toEqual([{ type: 'response', content: 'hi' }]);
  });

  it('多事件分多次 feed 依次取出', () => {
    const p = new SSEParser();
    p.feed('data: {"type":"thinking","content":"a"}\n');
    p.feed('data: {"type":"response","content":"b"}\ndata: {"type":"done"}\n');
    expect(p.takeEvents()).toEqual([
      { type: 'thinking', content: 'a' },
      { type: 'response', content: 'b' },
      { type: 'done' },
    ]);
  });

  it('忽略非 data: 行（注释、空行、事件行）', () => {
    const p = new SSEParser();
    p.feed(': 注释\n\nid: 1\nretry: 100\n');
    expect(p.takeEvents()).toEqual([]);
  });

  it('JSON 解析失败的行静默跳过，不影响后续', () => {
    const p = new SSEParser();
    p.feed('data: {bad json\n');
    expect(p.takeEvents()).toEqual([]);
    p.feed('data: {"type":"done"}\n');
    expect(p.takeEvents()).toEqual([{ type: 'done' }]);
  });

  it('flush 处理流末尾无换行的最后一行', () => {
    const p = new SSEParser();
    p.feed('data: {"type":"done"}');
    expect(p.takeEvents()).toEqual([]); // 未换行，仍在缓冲
    p.flush();
    expect(p.takeEvents()).toEqual([{ type: 'done' }]);
  });

  it('takeEvents 取出后清空队列', () => {
    const p = new SSEParser();
    p.feed('data: {"type":"done"}\n');
    p.takeEvents();
    expect(p.takeEvents()).toEqual([]);
  });
});

describe('dispatchSSEEvents', () => {
  it('路由聊天事件：thinking / response / done / error', () => {
    const cbs = {
      onThinking: vi.fn(),
      onResponse: vi.fn(),
      onDone: vi.fn(),
      onError: vi.fn(),
    };
    dispatchSSEEvents(
      [
        { type: 'thinking', stage: 'rag', content: '检索中' },
        { type: 'response', content: '你好', session_id: 's1' },
        { type: 'done', session_id: 's1' },
        { type: 'error', content: 'LLM 超时' },
      ],
      cbs,
    );
    expect(cbs.onThinking).toHaveBeenCalledWith('rag', '检索中', undefined);
    expect(cbs.onResponse).toHaveBeenCalledWith('你好', 's1');
    expect(cbs.onDone).toHaveBeenCalledWith('s1');
    expect(cbs.onError).toHaveBeenCalledWith('LLM 超时');
  });

  it('路由 Plan-and-Execute 事件', () => {
    const cbs = {
      onPlanStart: vi.fn(),
      onPlanStep: vi.fn(),
      onPlanStepEnd: vi.fn(),
      onPlanComplete: vi.fn(),
      onPlanFallback: vi.fn(),
      onPlanSynthesize: vi.fn(),
    };
    dispatchSSEEvents(
      [
        { type: 'plan_start', goal: '写报告', total_steps: 3 },
        { type: 'plan_step', step: 1, action: '搜索', status: 'done' },
        { type: 'plan_step_end', step: 1, result: '找到 5 条' },
        { type: 'plan_synthesize' },
        { type: 'plan_complete', total_steps: 3, completed_steps: 3 },
        { type: 'plan_fallback', reason: '步骤过多' },
      ],
      cbs,
    );
    expect(cbs.onPlanStart).toHaveBeenCalledWith('写报告', 3);
    expect(cbs.onPlanStep).toHaveBeenCalledWith(1, '搜索', 'done');
    expect(cbs.onPlanStepEnd).toHaveBeenCalledWith(1, '找到 5 条');
    expect(cbs.onPlanSynthesize).toHaveBeenCalledTimes(1);
    expect(cbs.onPlanComplete).toHaveBeenCalledWith(3, 3);
    expect(cbs.onPlanFallback).toHaveBeenCalledWith('步骤过多');
  });

  it('路由 reflection 反思事件（L1 综合后自检）', () => {
    const cbs = {
      onReflection: vi.fn(),
    };
    dispatchSSEEvents(
      [
        { type: 'reflection', stage: 'checking', round: 0 },
        { type: 'reflection', stage: 'refining', round: 1 },
      ],
      cbs,
    );
    expect(cbs.onReflection).toHaveBeenNthCalledWith(1, 'checking', 0);
    expect(cbs.onReflection).toHaveBeenNthCalledWith(2, 'refining', 1);
  });

  it('路由工具调用事件（含 tool_file）', () => {
    const cbs = {
      onToolStart: vi.fn(),
      onToolEnd: vi.fn(),
      onToolFile: vi.fn(),
    };
    dispatchSSEEvents(
      [
        { type: 'tool_start', name: 'fetch' },
        { type: 'tool_end', name: 'fetch', duration_ms: 1200 },
        { type: 'tool_file', file_id: 'abc', download_url: '/api/v1/ppt/abc' },
      ],
      cbs,
    );
    expect(cbs.onToolStart).toHaveBeenCalledWith('fetch');
    expect(cbs.onToolEnd).toHaveBeenCalledWith('fetch', 1200);
    expect(cbs.onToolFile).toHaveBeenCalledWith(expect.objectContaining({ file_id: 'abc' }));
  });

  it('路由知识库事件（event_type 字段）', () => {
    const cbs = { onKnowledgeProgress: vi.fn(), onError: vi.fn() };
    dispatchSSEEvents(
      [
        { event_type: 'processing', filename: 'a.pdf', progress: 50 },
        { event_type: 'completed', filename: 'a.pdf', progress: 100 },
        { event_type: 'error', message: '解析失败' },
      ],
      cbs,
    );
    expect(cbs.onKnowledgeProgress).toHaveBeenCalledTimes(2);
    expect(cbs.onKnowledgeProgress).toHaveBeenCalledWith(expect.objectContaining({ event_type: 'processing' }));
    expect(cbs.onError).toHaveBeenCalledWith('解析失败');
  });
});
