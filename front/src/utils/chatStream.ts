import { endpoints } from '../api/endpoints';
import { dispatchSSEEvents, SSEParser, type SSEDispatchCallbacks, type SSEEvent } from './sseParser';

const WORKFLOW_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const REPLAY_INTERVAL_MS = 1500;
const REPLAY_DEADLINE_MS = 60_000;
const BACKGROUND_REPLAY_DEADLINE_MS = 195_000;
const REPLAY_REQUEST_TIMEOUT_MS = 5_000;

interface StreamCursor {
  workflowId?: string;
  sequence: number;
  terminal: boolean;
}

interface EventPage {
  events: SSEEvent[];
  has_more: boolean;
  status: string;
}

function deliver(events: SSEEvent[], cursor: StreamCursor, callbacks: SSEDispatchCallbacks, signal: AbortSignal): void {
  for (const event of events) {
    if (cursor.terminal || signal.aborted) return;
    if (!event || typeof event !== 'object') continue;
    if (typeof event.workflow_id === 'string' && WORKFLOW_ID.test(event.workflow_id)) {
      if (cursor.workflowId && cursor.workflowId !== event.workflow_id) continue;
      cursor.workflowId = event.workflow_id;
    }
    if (typeof event.sequence === 'number' && Number.isSafeInteger(event.sequence) && event.sequence > 0) {
      if (event.sequence <= cursor.sequence) continue;
      if (event.sequence !== cursor.sequence + 1) throw new Error('事件序号不连续');
      cursor.sequence = event.sequence;
    }
    cursor.terminal = event.type === 'done' || event.type === 'error' ||
      event.event_type === 'finish' || event.event_type === 'error';
    dispatchSSEEvents([event], callbacks);
  }
}

function pause(signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    const timer = setTimeout(finish, REPLAY_INTERVAL_MS);
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    }
    signal.addEventListener('abort', finish, { once: true });
    if (signal.aborted) finish();
  });
}

async function replay(
  workflowId: string, cursor: StreamCursor, callbacks: SSEDispatchCallbacks,
  authorization: Record<string, string>, signal: AbortSignal, deadlineMs: number,
): Promise<void> {
  const replaySignal = AbortSignal.any([signal, AbortSignal.timeout(deadlineMs)]);
  while (!cursor.terminal && !replaySignal.aborted) {
    let page: EventPage | undefined;
    try {
      const response = await fetch(endpoints.chat.workflowEvents(workflowId, cursor.sequence), {
        headers: authorization, signal: AbortSignal.any([replaySignal, AbortSignal.timeout(REPLAY_REQUEST_TIMEOUT_MS)]),
        cache: 'no-store',
      });
      if (response.ok) page = (await response.json()).data as EventPage;
      else if (response.status < 500 && response.status !== 429) break;
    } catch {
      // A temporary network failure still permits a later read of the same cursor.
    }
    if (replaySignal.aborted) break;
    if (page) {
      const before = cursor.sequence;
      deliver(page.events, cursor, callbacks, replaySignal);
      if (cursor.terminal) return;
      if (page.has_more && cursor.sequence > before) continue;
      if (page.status !== 'running') break;
    }
    await pause(replaySignal);
  }
  if (!cursor.terminal && !signal.aborted) callbacks.onError?.('连接中断，未能读取剩余回复。');
}

export async function runChatStream(
  url: string, body: Record<string, unknown>, callbacks: SSEDispatchCallbacks, signal: AbortSignal, token: string | null,
): Promise<void> {
  const cursor: StreamCursor = { sequence: 0, terminal: false };
  const authorization: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const background = body.continue_on_disconnect === true;
  let cancelSent = false;
  function requestStop() {
    if (!background || cancelSent || cursor.terminal || !cursor.workflowId) return;
    cancelSent = true;
    void fetch(endpoints.chat.cancelWorkflow(cursor.workflowId), {
      method: 'POST', headers: authorization, keepalive: true,
    }).catch(() => {});
  }
  if (background) signal.addEventListener('abort', requestStop);
  try {
    await consumeStream(url, body, callbacks, signal, authorization, cursor, background, requestStop);
  } finally {
    if (background) signal.removeEventListener('abort', requestStop);
  }
}

async function consumeStream(
  url: string, body: Record<string, unknown>, callbacks: SSEDispatchCallbacks, signal: AbortSignal,
  authorization: Record<string, string>, cursor: StreamCursor, background: boolean, requestStop: () => void,
): Promise<void> {
  let errorMessage = '连接中断，未能读取回复。';
  try {
    const response = await fetch(url, {
      method: 'POST', body: JSON.stringify(body), signal,
      headers: { 'Content-Type': 'application/json', ...authorization },
    });
    if (!response.ok) {
      const result = background ? await response.json().catch(() => undefined) : undefined;
      if (!signal.aborted) callbacks.onError?.(typeof result?.message === 'string' ? result.message : `请求失败: ${response.status}`);
      return;
    }
    const workflow = response.headers.get('X-Workflow-Id');
    if (background && workflow && WORKFLOW_ID.test(workflow)) cursor.workflowId = workflow;
    if (signal.aborted) {
      requestStop();
      return;
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error('无法读取响应流');
    const decoder = new TextDecoder();
    const parser = new SSEParser();
    try {
      while (!signal.aborted) {
        const { done, value } = await reader.read();
        if (signal.aborted || done) break;
        parser.feed(decoder.decode(value, { stream: true }));
        deliver(parser.takeEvents(), cursor, callbacks, signal);
      }
      if (!signal.aborted) {
        parser.feed(decoder.decode());
        parser.flush();
        deliver(parser.takeEvents(), cursor, callbacks, signal);
      }
    } finally {
      reader.releaseLock();
    }
  } catch (error) {
    if (error instanceof Error) errorMessage = error.message;
  }
  if (signal.aborted || cursor.terminal) return;
  if (cursor.workflowId) {
    try {
      await replay(cursor.workflowId, cursor, callbacks, authorization, signal,
        background ? BACKGROUND_REPLAY_DEADLINE_MS : REPLAY_DEADLINE_MS);
    } catch {
      if (!signal.aborted) callbacks.onError?.('连接中断，未能读取剩余回复。');
    }
  } else {
    callbacks.onError?.(errorMessage);
  }
}
