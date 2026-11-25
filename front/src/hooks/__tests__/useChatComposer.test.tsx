// @vitest-environment jsdom
import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { AxiosResponse } from 'axios';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('../../api/sessions', () => ({ sessionsApi: { getMessages: vi.fn() } }));
vi.mock('../useSSE', () => ({ useSSE: () => ({ start: vi.fn(), stop: vi.fn() }) }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import '../../i18n';
import { toast } from 'sonner';
import { sessionsApi } from '../../api/sessions';
import { useSessionStore } from '../../stores/useSessionStore';
import { useChatComposer } from '../useChatComposer';
import type { ApiResponse, MessageListResponse } from '../../types/api';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
type HistoryResponse = AxiosResponse<ApiResponse<MessageListResponse>>;
let container: HTMLDivElement;
let root: Root;
let composer: ReturnType<typeof useChatComposer>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function history(sessionId: string): HistoryResponse {
  return { data: { code: 0, message: 'ok', request_id: 'test', data: {
    messages: [{ id: 1, session_id: sessionId, role: 'user', content: sessionId, created_at: '' }],
    has_more: false, next_cursor: null,
  } } } as HistoryResponse;
}

function Harness() {
  const state = useChatComposer();
  useEffect(() => { composer = state; }, [state]);
  return null;
}

beforeEach(async () => {
  vi.resetAllMocks();
  useSessionStore.getState().resetAll();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(<Harness />));
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

it('ignores a slow history response after switching from A to B', async () => {
  const a = deferred<HistoryResponse>();
  const b = deferred<HistoryResponse>();
  vi.mocked(sessionsApi.getMessages).mockImplementation(id => id === 'A' ? a.promise : b.promise);
  let requestA!: Promise<void>;
  let requestB!: Promise<void>;
  await act(async () => { requestA = composer.loadSession('A'); });
  await act(async () => { requestB = composer.loadSession('B'); });
  await act(async () => { b.resolve(history('B')); await requestB; });
  await act(async () => { a.resolve(history('A')); await requestA; });
  expect(useSessionStore.getState().currentSessionId).toBe('B');
  expect(useSessionStore.getState().messages[0].session_id).toBe('B');
  expect(composer.loadingMessages).toBe(false);
});

it('ignores stale 404 errors and keeps the newer request loading', async () => {
  const a = deferred<HistoryResponse>();
  const b = deferred<HistoryResponse>();
  vi.mocked(sessionsApi.getMessages).mockImplementation(id => id === 'A' ? a.promise : b.promise);
  let requestA!: Promise<void>;
  let requestB!: Promise<void>;
  await act(async () => { requestA = composer.loadSession('A'); });
  await act(async () => { requestB = composer.loadSession('B'); });
  await act(async () => { a.reject({ response: { status: 404 } }); await requestA; });
  expect(useSessionStore.getState().currentSessionId).toBe('B');
  expect(composer.loadingMessages).toBe(true);
  expect(toast.error).not.toHaveBeenCalled();
  await act(async () => { b.resolve(history('B')); await requestB; });
  expect(useSessionStore.getState().messages[0].session_id).toBe('B');
});

it('does not restore old history after starting a new chat', async () => {
  const old = deferred<HistoryResponse>();
  vi.mocked(sessionsApi.getMessages).mockReturnValue(old.promise);
  let request!: Promise<void>;
  await act(async () => { request = composer.loadSession('old'); });
  await act(async () => composer.handleNewChat());
  await act(async () => { old.resolve(history('old')); await request; });
  expect(useSessionStore.getState().currentSessionId).toBeNull();
  expect(useSessionStore.getState().messages).toEqual([]);
  expect(composer.loadingMessages).toBe(false);
});
