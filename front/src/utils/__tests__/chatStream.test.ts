import { afterEach, expect, it, vi } from 'vitest';
import { runChatStream } from '../chatStream';
import type { SSEEvent } from '../sseParser';

const workflowId = '11111111-2222-3333-4444-555555555555';
const query = '/api/v1/chat/query';
const progress = { workflow_id: workflowId, sequence: 1, type: 'thinking', stage: 'processing' };

function stream(events: SSEEvent[]): Response {
  return new Response(events.map(event => {
    const id = event.sequence === undefined ? '' : `id: ${event.sequence}\n`;
    return `${id}data: ${JSON.stringify(event)}\n\n`;
  }).join(''), { headers: { 'Content-Type': 'text/event-stream' } });
}

function page(events: SSEEvent[], status = 'succeeded', hasMore = false): Response {
  return Response.json({ data: { events, status, has_more: hasMore } });
}

afterEach(() => vi.unstubAllGlobals());

it('reconnects with a read-only cursor and replaces partial text with the committed answer', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(stream([progress, { type: 'response', content: 'first chunk' }]))
    .mockResolvedValueOnce(page([progress, { type: 'plan_step_end', sequence: 2, step: 1 }], 'running', true))
    .mockResolvedValueOnce(page([{ type: 'done', sequence: 3, session_id: 'session', replay_content: 'full answer' }]));
  vi.stubGlobal('fetch', fetcher);
  const response = vi.fn();
  const thinking = vi.fn();
  const done = vi.fn();
  const error = vi.fn();
  await runChatStream(query, { message: 'fixture' }, { onResponse: response, onThinking: thinking, onDone: done,
    onError: error }, new AbortController().signal, 'fixture-token');
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
    query, `/api/v1/chat/workflows/${workflowId}/events?after=1`,
    `/api/v1/chat/workflows/${workflowId}/events?after=2`,
  ]);
  expect(fetcher.mock.calls[0][1].method).toBe('POST');
  expect(fetcher.mock.calls[1][1]).toMatchObject({ headers: { Authorization: 'Bearer fixture-token' } });
  expect(fetcher.mock.calls[1][1].method).toBeUndefined();
  expect(thinking).toHaveBeenCalledTimes(1);
  expect(response.mock.calls).toEqual([['first chunk', undefined], ['full answer', 'session', true]]);
  expect(done).toHaveBeenCalledOnce();
  expect(error).not.toHaveBeenCalled();
});

it('reports a disconnected run that has no committed reply as an error', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(stream([progress]))
    .mockResolvedValueOnce(page([{ type: 'error', sequence: 2, content: '原对话已停止' }], 'cancelled'));
  vi.stubGlobal('fetch', fetcher);
  const onError = vi.fn();
  const onDone = vi.fn();
  await runChatStream(query, {}, { onError, onDone }, new AbortController().signal, null);
  expect(onError).toHaveBeenCalledWith('原对话已停止');
  expect(onDone).not.toHaveBeenCalled();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it('keeps explicit stop silent and never enters recovery', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(stream([progress, { type: 'response', content: 'late chunk' }]));
  vi.stubGlobal('fetch', fetcher);
  const controller = new AbortController();
  const onError = vi.fn();
  const onResponse = vi.fn();
  await runChatStream(query, {}, { onThinking: () => controller.abort(), onError, onResponse }, controller.signal, null);
  expect(onError).not.toHaveBeenCalled();
  expect(onResponse).not.toHaveBeenCalled();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('explicitly cancels the accepted detached run with a separate request', async () => {
  const live = stream([progress]);
  live.headers.set('X-Workflow-Id', workflowId);
  const fetcher = vi.fn().mockResolvedValueOnce(live).mockResolvedValueOnce(Response.json({ data: { status: 'cancelled' } }));
  vi.stubGlobal('fetch', fetcher);
  const control = new AbortController();
  await runChatStream(query, { continue_on_disconnect: true }, {
    onThinking: () => control.abort(),
  }, control.signal, 'fixture-token');
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls[1]).toEqual([`/api/v1/chat/workflows/${workflowId}/cancel`, {
    headers: { Authorization: 'Bearer fixture-token' }, method: 'POST', keepalive: true,
  }]);
});

it('reads a detached result after transport loss before the first frame without cancelling or rerunning', async () => {
  const live = stream([]);
  live.headers.set('X-Workflow-Id', workflowId);
  const fetcher = vi.fn().mockResolvedValueOnce(live)
    .mockResolvedValueOnce(page([{ type: 'done', sequence: 1, replay_content: 'saved answer' }]));
  vi.stubGlobal('fetch', fetcher);
  const onResponse = vi.fn();
  await runChatStream(query, { continue_on_disconnect: true }, { onResponse }, new AbortController().signal, null);
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([query, `/api/v1/chat/workflows/${workflowId}/events?after=0`]);
  expect(onResponse).toHaveBeenCalledWith('saved answer', undefined, true);
});
