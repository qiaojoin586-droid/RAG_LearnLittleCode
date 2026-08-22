/**
 * SSE 流解析工具（纯函数，无 React 依赖，便于单元测试）
 *
 * 背景（审查 M6）：useSSE 与 KnowledgeBase 各有一份手写 SSE 解析逻辑，
 * 此处收敛为可复用的增量解析器 + 事件分发器，并为后续 Vitest 测试网提供
 * 可测单元。
 *
 * 解析规则（与生产行为保持一致）：
 * - 按 \n 拆行，不完整的最后一行保留到下一次 feed
 * - 只处理 `data: ` 前缀行（SSE 规范的 data 字段）
 * - 每行内容为 JSON，解析失败静默跳过
 */

/** SSE 事件（JSON 反序列化后的对象） */
export interface SSEEvent {
  type?: string;
  event_type?: string;
  [key: string]: unknown;
}

/**
 * 增量 SSE 行解析器
 *
 * 用法：
 *   const parser = new SSEParser();
 *   parser.feed(chunkText);            // 可多次调用（分块到达）
 *   const events = parser.takeEvents();// 取走已解析出的完整事件
 *   parser.flush();                    // 流结束时调用，处理残留最后一行
 */
export class SSEParser {
  private buffer = '';
  private events: SSEEvent[] = [];

  /** 喂入一个文本块（chunk），内部按行拆分解析 */
  feed(chunk: string): void {
    this.buffer += chunk;
    const lines = this.buffer.split('\n');
    // 最后一行可能不完整（chunk 边界处被切断），保留到下次
    this.buffer = lines.pop() || '';

    for (const line of lines) {
      this.parseLine(line);
    }
  }

  /** 流结束时调用：处理缓冲区中残留的最后一行 */
  flush(): void {
    if (this.buffer.trim()) {
      this.parseLine(this.buffer);
      this.buffer = '';
    }
  }

  /** 取走已解析的事件（取出后清空内部队列） */
  takeEvents(): SSEEvent[] {
    const events = this.events;
    this.events = [];
    return events;
  }

  private parseLine(line: string): void {
    if (!line.startsWith('data: ')) return;
    try {
      this.events.push(JSON.parse(line.slice(6)) as SSEEvent);
    } catch {
      // JSON 解析失败，跳过该行
    }
  }
}

/** SSE 回调配置（与 useSSE 对齐，供纯函数分发使用） */
export interface SSEDispatchCallbacks {
  onThinking?: (stage: string, content: string, details?: Record<string, unknown>) => void;
  onResponse?: (content: string, sessionId?: string) => void;
  onDone?: (sessionId?: string) => void;
  onError?: (message: string) => void;
  onKnowledgeProgress?: (data: SSEEvent) => void;
  onPlanStart?: (goal: string, totalSteps: number) => void;
  onPlanStep?: (step: number, action: string, status: string) => void;
  onPlanStepStart?: (step: number, action: string) => void;
  onPlanStepEnd?: (step: number, result: string) => void;
  onPlanSynthesize?: () => void;
  onPlanComplete?: (totalSteps: number, completedSteps: number) => void;
  onPlanFallback?: (reason: string) => void;
  onReflection?: (stage: string, round: number) => void;
  onToolStart?: (name: string) => void;
  onToolEnd?: (name: string, durationMs?: number) => void;
  onToolFile?: (info: SSEEvent) => void;
}

/**
 * 分发一组 SSE 事件到对应回调（按 type / event_type 路由）
 *
 * 聊天事件：thinking / response / done / error
 * Plan-and-Execute：plan_start / plan_step / plan_step_start / plan_step_end /
 *                   plan_synthesize / plan_complete / plan_fallback
 * 工具调用：tool_start / tool_end / tool_file
 * 知识库事件：event_type = processing / completed / finish / error
 */
export function dispatchSSEEvents(events: SSEEvent[], callbacks: SSEDispatchCallbacks): void {
  for (const data of events) {
    // 知识库 SSE 事件（通过 event_type 字段区分）
    if ('event_type' in data && data.event_type) {
      if (data.event_type === 'processing' || data.event_type === 'completed' || data.event_type === 'finish') {
        callbacks.onKnowledgeProgress?.(data);
      } else if (data.event_type === 'error') {
        callbacks.onError?.(String(data.message || '上传失败'));
      }
      continue;
    }

    // 聊天 SSE 事件
    switch (data.type) {
      case 'thinking':
        callbacks.onThinking?.(String(data.stage || ''), String(data.content || ''), (data.details as Record<string, unknown>) || undefined);
        break;
      case 'response':
        if (data.content) {
          callbacks.onResponse?.(String(data.content), data.session_id ? String(data.session_id) : undefined);
        }
        break;
      case 'done':
        callbacks.onDone?.(data.session_id ? String(data.session_id) : undefined);
        break;
      case 'error':
        callbacks.onError?.(String(data.content || '未知错误'));
        break;
      case 'plan_start':
        callbacks.onPlanStart?.(String(data.goal || ''), Number(data.total_steps || 0));
        break;
      case 'plan_step':
        callbacks.onPlanStep?.(Number(data.step || 0), String(data.action || ''), String(data.status || 'pending'));
        break;
      case 'plan_step_start':
        callbacks.onPlanStepStart?.(Number(data.step || 0), String(data.action || ''));
        break;
      case 'plan_step_end':
        callbacks.onPlanStepEnd?.(Number(data.step || 0), String(data.result || ''));
        break;
      case 'plan_synthesize':
        callbacks.onPlanSynthesize?.();
        break;
      case 'plan_complete':
        callbacks.onPlanComplete?.(Number(data.total_steps || 0), Number(data.completed_steps || 0));
        break;
      case 'plan_fallback':
        callbacks.onPlanFallback?.(String(data.reason || ''));
        break;
      case 'reflection':
        callbacks.onReflection?.(String(data.stage || ''), Number(data.round || 0));
        break;
      case 'tool_start':
        callbacks.onToolStart?.(String(data.name || ''));
        break;
      case 'tool_end':
        callbacks.onToolEnd?.(String(data.name || ''), data.duration_ms === undefined ? undefined : Number(data.duration_ms));
        break;
      case 'tool_file':
        callbacks.onToolFile?.(data);
        break;
    }
  }
}
