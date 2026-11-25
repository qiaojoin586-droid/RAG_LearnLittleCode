// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { modelConfigApi, type OutboxEvent, type OutboxRedrive } from '../../api/modelConfig';
import ModelConfigOutbox from '../ModelConfigOutbox';

vi.mock('../../api/modelConfig', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/modelConfig')>();
  return { ...actual, modelConfigApi: { outbox: vi.fn(), outboxEvent: vi.fn(), redriveOutbox: vi.fn() } };
});
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement; let root: Root;
const proof = vi.fn<() => Promise<string | null>>();
const event: OutboxEvent = { id: 'source-event', event_type: 'credential.revoked', aggregate_id: 'credential', aggregate_version: 1,
  status: 'dead_letter', attempts: 5, next_attempt_at: '2026-11-17T12:00:00', lease_expires_at: null,
  error_code: 'CONFIG_NOTIFY_FAILED', created_at: '2026-11-17T12:00:00', redrive_event_id: null };
const result: OutboxRedrive = { source_event_id: event.id, event_id: 'child-event', status: 'pending', attempts: 0,
  original_attempts: 5, delivery_guarantee: 'at_least_once', config_head_changed: false };
beforeEach(() => {
  vi.resetAllMocks(); window.localStorage.clear(); window.sessionStorage.clear();
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
  vi.mocked(modelConfigApi.outbox).mockResolvedValue({ items: [event] });
  vi.mocked(modelConfigApi.outboxEvent).mockResolvedValue(event);
  vi.mocked(modelConfigApi.redriveOutbox).mockResolvedValue(result); proof.mockResolvedValue('proof-one');
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
async function mount() { await act(async () => root.render(<ModelConfigOutbox askProof={proof} />)); container.querySelector('details')!.open = true; }
function button(text: string) { return [...document.querySelectorAll('button')].find(node => node.textContent === text)!; }
async function load() { await act(async () => button('刷新通知').click()); }
async function inspect() { await act(async () => button('确认并补投').click()); }
async function confirm() {
  await act(async () => (document.querySelector('[role=dialog] input[type=checkbox]') as HTMLInputElement).click());
  await act(async () => button('确认补投并验证身份').click());
}

it('loads only on request, paginates and refreshes with explicit status filter', async () => {
  vi.mocked(modelConfigApi.outbox).mockResolvedValueOnce({ items: [event], next_cursor: 'cursor' })
    .mockResolvedValueOnce({ items: [{ ...event, id: 'second-event' }] });
  await mount(); expect(modelConfigApi.outbox).not.toHaveBeenCalled(); await load();
  expect(vi.mocked(modelConfigApi.outbox).mock.calls[0].slice(0, 2)).toEqual(['dead_letter', undefined]);
  await act(async () => button('更多通知').click());
  expect(vi.mocked(modelConfigApi.outbox).mock.calls[1].slice(0, 2)).toEqual(['dead_letter', 'cursor']);
  const select = container.querySelector('select')!;
  await act(async () => { select.value = 'succeeded'; select.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.querySelectorAll('article')).toHaveLength(0);
  vi.mocked(modelConfigApi.outbox).mockResolvedValueOnce({ items: [] }); await load();
  expect(container.textContent).toContain('当前筛选下没有配置通知');
  expect(vi.mocked(modelConfigApi.outbox).mock.lastCall!.slice(0, 2)).toEqual(['succeeded', undefined]);
});

it('requires confirmation and reauth, preserves dead letter and distinguishes receipt from delivery', async () => {
  await mount(); await load(); await inspect();
  expect(button('确认补投并验证身份').disabled).toBe(true);
  expect(document.body.textContent).toContain('不会重新发布配置、恢复凭据或重新调用模型');
  await confirm(); expect(proof).toHaveBeenCalledOnce();
  expect(modelConfigApi.redriveOutbox).toHaveBeenCalledWith(event.id, 5, expect.any(String), 'proof-one');
  expect(container.textContent).toContain('补投事件已创建，等待后台处理');
  expect(container.textContent).toContain('死信'); expect(container.textContent).toContain('5 / 5');
  vi.mocked(modelConfigApi.outboxEvent).mockResolvedValueOnce({ ...event, id: 'child-event', status: 'pending', attempts: 0, error_code: null });
  await act(async () => button('查看补投状态').click());
  expect(document.querySelector('[role=dialog]')!.textContent).toContain('待处理');
  expect(button('确认补投并验证身份')).toBeUndefined();
  expect(localStorage.length + sessionStorage.length).toBe(0);
});

it('does not mutate when confirmation or reauth is dismissed', async () => {
  await mount(); await load(); await inspect(); await act(async () => button('关闭').click());
  expect(proof).not.toHaveBeenCalled(); await inspect(); proof.mockResolvedValueOnce(null); await confirm();
  expect(modelConfigApi.redriveOutbox).not.toHaveBeenCalled();
});

it('retries a failed submission with the same idempotency key and a new proof', async () => {
  vi.mocked(modelConfigApi.redriveOutbox).mockRejectedValueOnce(new Error('network'));
  await mount(); await load(); await inspect(); await confirm();
  expect(container.querySelector('[role=alert]')).not.toBeNull();
  proof.mockResolvedValueOnce('proof-two'); await inspect(); await confirm();
  const calls = vi.mocked(modelConfigApi.redriveOutbox).mock.calls;
  expect(calls[0][2]).toBe(calls[1][2]); expect(calls[1][3]).toBe('proof-two');
});

it('rechecks state before offering redrive and can navigate an existing redrive chain', async () => {
  await mount(); await load();
  vi.mocked(modelConfigApi.outboxEvent).mockResolvedValueOnce({ ...event, redrive_event_id: 'child-event' });
  await inspect(); expect(button('确认补投并验证身份')).toBeUndefined();
  vi.mocked(modelConfigApi.outboxEvent).mockResolvedValueOnce({ ...event, id: 'child-event', status: 'succeeded', attempts: 1 });
  await act(async () => button('查看该次补投状态').click());
  expect(document.querySelector('[role=dialog]')!.textContent).toContain('已送达');
  expect(modelConfigApi.redriveOutbox).not.toHaveBeenCalled();
});

it('shows a refresh instruction when server rejects stale state', async () => {
  vi.mocked(modelConfigApi.redriveOutbox).mockRejectedValueOnce({ response: { status: 409, data: { code: 40923, data: { reason: 'OUTBOX_STATE_CHANGED' } } } });
  await mount(); await load(); await inspect(); await confirm();
  expect(container.querySelector('[role=alert]')!.textContent).toContain('请刷新通知后核对');
  expect(container.textContent).not.toContain('补投事件已创建');
});

it('aborts pending reads and ignores late results after unmount', async () => {
  let resolve!: (value: OutboxEvent) => void;
  vi.mocked(modelConfigApi.outboxEvent).mockReturnValueOnce(new Promise(done => { resolve = done; }));
  await mount(); await load(); await act(async () => button('查看通知').click());
  const signal = vi.mocked(modelConfigApi.outboxEvent).mock.calls[0][1]!;
  await act(async () => root.render(null)); expect(signal.aborted).toBe(true);
  await act(async () => resolve(event)); expect(document.querySelector('[role=dialog]')).toBeNull();
});
