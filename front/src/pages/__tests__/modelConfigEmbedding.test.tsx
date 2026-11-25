// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { modelConfigApi, type EmbeddingStatus } from '../../api/modelConfig';
import ModelConfigEmbedding from '../ModelConfigEmbedding';

vi.mock('../../api/modelConfig', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/modelConfig')>();
  return { ...actual, modelConfigApi: { embeddingStatus: vi.fn() } };
});
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement; let root: Root;
const empty: EmbeddingStatus = { items: [], draft: null, head_version: 0,
  registration_status: 'not_registered', migration_supported: false, runtime_binding_supported: false };
beforeEach(() => {
  vi.resetAllMocks(); container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
  vi.mocked(modelConfigApi.embeddingStatus).mockResolvedValue(empty);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
async function mount() { await act(async () => root.render(<ModelConfigEmbedding draftId="draft" />)); }

it('explains an unregistered store without offering an unavailable migration action', async () => {
  await mount();
  expect(container.textContent).toContain('尚未登记现有集合');
  expect(container.textContent).toContain('当前发布仍需停用向量角色');
  expect([...container.querySelectorAll('button')].map(button => button.textContent)).toEqual(['刷新索引状态']);
});

it('keeps observed dimension and record count separate from verified or usable status', async () => {
  vi.mocked(modelConfigApi.embeddingStatus).mockResolvedValue({ ...empty, registration_status: 'registered',
    items: [{ id: 'index', index_key: 'rag', index_version: 2, collection_name: 'rag_collection', dimension: 1024,
      metadata_record_count: 7, semantic_provenance_verified: false, runtime_usable: false, status: 'baseline_unverified', created_at: '' }],
    draft: { id: 'draft', draft_version: 4, embedding_enabled: true, publication_blockers: [],
      embedding_diff: [{ index_key: 'rag', status: 'pending_rebuild', reason: 'EMBEDDING_SPACE_MISMATCH' }] } });
  await mount();
  expect(container.textContent).toContain('观测维度：1024；目录记录数：7');
  expect(container.textContent).toContain('待核验：缺少建库模型来源证据');
  expect(container.textContent).toContain('属于不同向量空间');
  expect(container.textContent).not.toContain('当前发布使用此索引');
});

it('shows an active attested binding while keeping new-space migration unavailable', async () => {
  vi.mocked(modelConfigApi.embeddingStatus).mockResolvedValue({ ...empty, registration_status: 'registered', runtime_binding_supported: true,
    items: [{ id: 'index', index_key: 'notes', index_version: 2, collection_name: 'notes_collection', dimension: 1024,
      metadata_record_count: null, semantic_provenance_verified: true, runtime_usable: true, status: 'baseline_verified', created_at: '' }] });
  await mount();
  expect(container.textContent).toContain('已由管理员核验构建记录');
  expect(container.textContent).toContain('当前发布使用此索引');
  expect(container.textContent).toContain('同空间换密钥或超时');
  expect(container.textContent).not.toContain('当前发布仍需停用向量角色');
  expect(container.textContent).toContain('新向量空间的索引重建与切换尚未开放');
});

it('shows a retryable error and fetches fresh data on refresh', async () => {
  vi.mocked(modelConfigApi.embeddingStatus).mockRejectedValueOnce({ response: { status: 403 } });
  await mount(); expect(container.querySelector('[role=alert]')).not.toBeNull();
  await act(async () => container.querySelector('button')!.click());
  expect(modelConfigApi.embeddingStatus).toHaveBeenCalledTimes(2);
  expect(container.querySelector('[role=alert]')).toBeNull();
  expect(container.textContent).toContain('尚未登记现有集合');
});

it('aborts an old read and ignores late data after the role is closed', async () => {
  let resolve!: (value: EmbeddingStatus) => void;
  vi.mocked(modelConfigApi.embeddingStatus).mockReturnValue(new Promise(done => { resolve = done; }));
  await mount(); expect(container.querySelector('[aria-busy=true]')).not.toBeNull();
  const signal = vi.mocked(modelConfigApi.embeddingStatus).mock.calls[0][1]!;
  await act(async () => root.render(null)); expect(signal.aborted).toBe(true);
  await act(async () => resolve(empty)); expect(container.textContent).toBe('');
});
