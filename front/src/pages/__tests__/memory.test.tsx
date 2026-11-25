// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { memoryApi, type MemoryDocument, type MemoryJob } from '../../api/memory';
import Memory from '../Memory';

let owner = 'first';
vi.mock('../../stores/useUserStore', () => ({ useUserStore: (select: (state: unknown) => unknown) => select({ userInfo: { username: owner } }) }));
vi.mock('../../api/memory', async original => ({ ...await original<typeof import('../../api/memory')>(),
  memoryApi: { get: vi.fn(), save: vi.fn(), settings: vi.fn(), revisions: vi.fn(), revision: vi.fn(), restore: vi.fn(),
    clear: vi.fn(), jobs: vi.fn(), generate: vi.fn(), apply: vi.fn(), dismiss: vi.fn(), download: vi.fn() } }));
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement;
let root: Root;
const doc = (overrides: Partial<MemoryDocument> = {}): MemoryDocument => ({ exists: true, filename: 'memory.md', revision_id: 'revision',
  revision_number: 1, head_version: 1, memory_epoch: 0, content_md: '# 中文偏好', content_hash: 'hash',
  settings: { enabled: true, auto_extract: true, management_mode: 'manual' }, extraction_available: true, updated_at: null, ...overrides });
const proposal: MemoryJob = { id: 'job', status: 'proposed', generation_number: 1, source_message_id: 5,
  proposed_content_md: '# 私人候选正文', error_reason: null, applied_revision_id: null, created_at: '2026-11-16T00:00:00Z' };
beforeEach(() => {
  vi.resetAllMocks(); owner = 'first';
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  vi.mocked(memoryApi.get).mockResolvedValue(doc()); vi.mocked(memoryApi.revisions).mockResolvedValue({ items: [] });
  vi.mocked(memoryApi.jobs).mockResolvedValue({ items: [] });
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); });
const button = (text: string) => [...document.querySelectorAll('button')].find(item => item.textContent?.includes(text))!;
async function click(text: string) { await act(async () => button(text).click()); }
async function mount() { await act(async () => root.render(<Memory />)); }
async function edit(value: string) {
  await act(async () => {
    const textarea = container.querySelector('textarea')!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, value);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('shows a personal Markdown file and byte budget without requesting config permission', async () => {
  await mount(); expect(container.querySelector('textarea')?.value).toBe('# 中文偏好');
  expect(container.textContent).toContain('14 / 32768 字节');
  expect(container.textContent).toContain('手工管理，AI 只给建议');
  expect(memoryApi.get).toHaveBeenCalledTimes(1);
});

it('keeps unsaved Markdown and the replay key on a CAS conflict', async () => {
  vi.mocked(memoryApi.save).mockRejectedValue({ response: { data: { code: 40940 } } });
  await mount(); await edit('# 未保存偏好'); await click('保存，下一轮生效');
  expect(container.querySelector('textarea')?.value).toBe('# 未保存偏好');
  expect(container.textContent).toContain('你的未保存内容已保留');
  await click('保存，下一轮生效');
  expect(vi.mocked(memoryApi.save).mock.calls[0][2]).toBe(vi.mocked(memoryApi.save).mock.calls[1][2]);
});

it('preserves disabled settings on save and reports a committed write despite a refresh failure', async () => {
  const disabled = doc({ settings: { enabled: false, auto_extract: false, management_mode: 'manual' } });
  vi.mocked(memoryApi.get).mockResolvedValue(disabled);
  await mount(); await edit('新的中文偏好');
  vi.mocked(memoryApi.save).mockResolvedValue({ ...disabled, content_md: '新的中文偏好', head_version: 2 });
  vi.mocked(memoryApi.revisions).mockRejectedValue(new Error('offline'));
  await click('保存，下一轮生效');
  expect(memoryApi.settings).not.toHaveBeenCalled(); expect(container.textContent).toContain('已停用');
  expect(container.textContent).toContain('已保存；历史或任务状态暂未刷新');
  expect(button('保存，下一轮生效').disabled).toBe(true);
});

it('renders Markdown without executing HTML, active links or loading remote images', async () => {
  vi.mocked(memoryApi.get).mockResolvedValue(doc({ content_md: '<script>alert(1)</script>\n\n[链接](javascript:alert(1))\n\n![x](https://external.example/pixel)' }));
  await mount(); await click('预览');
  expect(container.querySelector('script,img,a')).toBeNull();
  expect(container.textContent).toContain('外部图片未自动加载');
});

it('shows a proposal as read-only and removes it after adoption', async () => {
  vi.mocked(memoryApi.jobs).mockResolvedValueOnce({ items: [proposal] }).mockResolvedValue({ items: [{ ...proposal, status: 'applied', proposed_content_md: null }] });
  vi.mocked(memoryApi.apply).mockResolvedValue(doc({ content_md: proposal.proposed_content_md, head_version: 2 }));
  await mount(); expect(container.querySelector('textarea')?.value).toBe('# 中文偏好');
  expect(container.textContent).toContain('私人候选正文');
  await click('采用建议'); expect(container.querySelector('textarea')?.value).toBe(proposal.proposed_content_md);
  expect(container.querySelector('.memory-diff')).toBeNull();
});

it('confirms purge and destroys displayed candidate text even when follow-up reads fail', async () => {
  vi.mocked(memoryApi.jobs).mockResolvedValueOnce({ items: [proposal] }).mockRejectedValue(new Error('offline'));
  vi.mocked(memoryApi.clear).mockResolvedValue(doc({ content_md: '', head_version: 2, memory_epoch: 1,
    settings: { enabled: false, auto_extract: false, management_mode: 'manual' } }));
  await mount(); await click('清空并停用');
  expect(memoryApi.clear).not.toHaveBeenCalled();
  const checkbox = document.querySelector('.memory-clear-checkbox input') as HTMLInputElement;
  await act(async () => checkbox.click()); await click('确认清空并停用');
  expect(memoryApi.clear).toHaveBeenCalledWith(expect.objectContaining({ head_version: 1 }), true, expect.any(String));
  expect(container.textContent).not.toContain('私人候选正文'); expect(container.querySelector('textarea')?.value).toBe('');
});

it('destroys editor plaintext immediately when the logged-in owner changes', async () => {
  await mount(); await edit('第一个用户的私密草稿');
  vi.mocked(memoryApi.get).mockReturnValue(new Promise(() => {})); owner = 'second';
  await mount(); expect(container.querySelector('textarea')).toBeNull();
  expect(container.textContent).not.toContain('第一个用户');
});

it('blocks a multi-byte file over the byte limit', async () => {
  await mount(); await edit('汉'.repeat(10923));
  expect(button('保存，下一轮生效').disabled).toBe(true);
  expect(container.textContent).toContain('32769 / 32768 字节');
});
