// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { AxiosHeaders, type AxiosResponse } from 'axios';
import { beforeEach, expect, it, vi } from 'vitest';
import { knowledgeApi } from '../../api/knowledge';
import type { ApiResponse, KnowledgeDocumentListResponse, KnowledgeUploadJob } from '../../types/api';
import KnowledgeBase from '../KnowledgeBase';

vi.mock('../../api/knowledge', () => ({
  knowledgeApi: { upload: vi.fn(), getUploadJob: vi.fn(), listUploadJobs: vi.fn(), listDocuments: vi.fn() },
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

function ok<T>(data: T): AxiosResponse<ApiResponse<T>> {
  return { status: 200, statusText: 'OK', headers: {}, config: { headers: new AxiosHeaders() },
    data: { code: 0, message: 'ok', request_id: 'knowledge-fixture', data } };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(knowledgeApi.listUploadJobs).mockResolvedValue(ok({ jobs: [], next_cursor: null }));
  vi.mocked(knowledgeApi.listDocuments).mockResolvedValue(ok<KnowledgeDocumentListResponse>({ documents: [], total: 0 }));
});

it('reads the durable job when the upload progress stream ends early', async () => {
  const jobId = '11111111-2222-3333-4444-555555555555';
  vi.mocked(knowledgeApi.upload).mockResolvedValue(new Response(
    'data: {"event_type":"processing","stage":"parsing","progress":30}\n\n',
    { headers: { 'X-Knowledge-Job-Id': jobId } },
  ));
  vi.mocked(knowledgeApi.getUploadJob).mockResolvedValue(ok<KnowledgeUploadJob>({
    job_id: jobId, filename: 'fixture.txt', document_id: 1,
    status: 'succeeded', stage: 'complete', progress: 100, error_code: null,
  }));

  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<KnowledgeBase />));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(input, 'files', { value: [new File(['source'], 'fixture.txt')] });
    await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
    expect(knowledgeApi.upload).toHaveBeenCalledTimes(1);
    expect(knowledgeApi.getUploadJob).toHaveBeenCalledWith(jobId, expect.any(AbortSignal));
    expect(container.textContent).toContain('100% · 完成');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it('recovers active jobs on page entry and keeps trying through a backend restart', async () => {
  vi.useFakeTimers();
  const jobId = '11111111-2222-3333-4444-555555555555';
  const secondId = '22222222-2222-3333-4444-555555555555';
  const queued: KnowledgeUploadJob = {
    job_id: jobId, filename: 'recover.txt', document_id: 1,
    status: 'running', stage: 'parsing', progress: 30, error_code: null,
  };
  const rejected: KnowledgeUploadJob = {
    ...queued, job_id: secondId, filename: 'invalid.pdf', status: 'failed',
    error_code: 'PDF_PARSE_FAILED', message: 'PDF 无法解析',
  };
  vi.mocked(knowledgeApi.listUploadJobs)
    .mockResolvedValueOnce(ok({ jobs: [queued], next_cursor: jobId }))
    .mockResolvedValueOnce(ok({ jobs: [rejected], next_cursor: null }));
  vi.mocked(knowledgeApi.getUploadJob)
    .mockRejectedValueOnce({ isAxiosError: true, response: { status: 503 } })
    .mockResolvedValueOnce(ok({ ...queued, status: 'succeeded', progress: 100, stage: 'complete' }));

  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<KnowledgeBase />));
    expect(knowledgeApi.listUploadJobs).toHaveBeenCalledWith(jobId, expect.any(AbortSignal));
    expect(knowledgeApi.upload).not.toHaveBeenCalled();
    expect(container.textContent).toContain('knowledge.reconnecting');
    expect(container.textContent).toContain('PDF 无法解析');
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(container.textContent).toContain('100% · 完成');
    expect(knowledgeApi.getUploadJob).toHaveBeenCalledTimes(2);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
  }
});

it('uses one progress watcher and row for repeated uploads returning the same job', async () => {
  const jobId = '11111111-2222-3333-4444-555555555555';
  vi.mocked(knowledgeApi.upload).mockImplementation(async () =>
    new Response(null, { headers: { 'X-Knowledge-Job-Id': jobId } }));
  let finish: (response: AxiosResponse<ApiResponse<KnowledgeUploadJob>>) => void = () => {};
  vi.mocked(knowledgeApi.getUploadJob).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<KnowledgeBase />));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(input, 'files', { value: [new File(['same'], 'first.txt'), new File(['same'], 'second.txt')] });
    await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
    expect(knowledgeApi.upload).toHaveBeenCalledTimes(2);
    expect(knowledgeApi.getUploadJob).toHaveBeenCalledTimes(1);
    expect(container.querySelectorAll('[role="status"]')).toHaveLength(1);
    await act(async () => finish(ok({
      job_id: jobId, filename: 'first.txt', document_id: 1,
      status: 'succeeded', stage: 'complete', progress: 100, error_code: null,
    })));
    expect(container.textContent).toContain('100% · 完成');
    expect(container.textContent).not.toContain('second.txt');
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
