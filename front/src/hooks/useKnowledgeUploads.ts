import { useCallback, useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { knowledgeApi } from '../api/knowledge';
import type { KnowledgeUploadJob } from '../types/api';
import { SSEParser } from '../utils/sseParser';

interface UploadProgress {
  id: string;
  filename: string;
  progress: number;
  stage: string;
  status: 'uploading' | 'completed' | 'error';
  message?: string;
}

function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    }
    signal.addEventListener('abort', done, { once: true });
  });
}

async function retryRead<T>(read: () => Promise<T>, signal: AbortSignal, reconnect?: () => void): Promise<T> {
  let delay = 1000;
  while (!signal.aborted) {
    try {
      return await read();
    } catch (error) {
      if (signal.aborted || !isAxiosError(error)) throw error;
      const status = error.response?.status;
      if (status !== undefined && status < 500 && status !== 408 && status !== 429) throw error;
      reconnect?.();
      await wait(delay, signal);
      delay = Math.min(delay * 2, 8000);
    }
  }
  throw signal.reason;
}

function fromJob(job: KnowledgeUploadJob): UploadProgress {
  const completed = job.status === 'succeeded';
  const failed = job.status === 'failed' || job.status === 'cancelled';
  return {
    id: job.job_id, filename: job.filename, progress: job.progress, stage: job.stage,
    status: completed ? 'completed' : failed ? 'error' : 'uploading',
    message: job.message ?? (job.status === 'cancelled' ? '文档已删除' : '入库失败'),
  };
}

async function readUploadReceipt(response: Response, signal: AbortSignal): Promise<string | null> {
  const jobId = response.headers.get('X-Knowledge-Job-Id');
  if (jobId) {
    void response.body?.cancel().catch(() => {});
    return jobId;
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error('无法读取上传确认');
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      parser.feed(decoder.decode(value, { stream: !done }));
      if (done) parser.flush();
      for (const event of parser.takeEvents()) {
        if (typeof event.job_id === 'string') return event.job_id;
        if (event.event_type === 'completed') return null;
        if (event.event_type === 'error') {
          throw new Error(typeof event.message === 'string' ? event.message : '入库失败');
        }
      }
      if (done) break;
    }
    throw new Error('上传连接中断');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function useKnowledgeUploads(refreshDocuments: (signal: AbortSignal) => Promise<void>) {
  const [uploads, setUploads] = useState<UploadProgress[]>([]);
  const running = useRef(new Map<string, AbortController>());
  const show = useCallback((row: UploadProgress, replaceId = row.id) => {
    setUploads((previous) => {
      const index = previous.findIndex((item) => item.id === replaceId || item.id === row.id);
      if (index < 0) return [...previous, row];
      return previous.flatMap((item, position) => position === index ? [row] :
        item.id === replaceId || item.id === row.id ? [] : [item]);
    });
  }, []);

  const watch = useCallback((initial: UploadProgress, replaceId?: string) => {
    const id = initial.id;
    if (running.current.has(id)) {
      if (replaceId) setUploads((previous) => previous.filter((item) => item.id !== replaceId));
      return;
    }
    show(initial, replaceId);
    if (initial.status !== 'uploading') return;
    const controller = new AbortController();
    running.current.set(id, controller);
    void (async () => {
      let current = initial;
      try {
        while (!controller.signal.aborted) {
          const result = await retryRead(() => knowledgeApi.getUploadJob(id, controller.signal), controller.signal,
            () => show({ ...current, stage: 'reconnecting' }));
          if (controller.signal.aborted) return;
          current = fromJob(result.data.data);
          show(current);
          if (current.status !== 'uploading') {
            if (current.status === 'completed') await refreshDocuments(controller.signal);
            return;
          }
          await wait(1500, controller.signal);
        }
      } catch {
        if (!controller.signal.aborted) show({ ...current, status: 'error', message: '无法查询上传任务' });
      } finally {
        if (running.current.get(id) === controller) running.current.delete(id);
      }
    })();
  }, [show, refreshDocuments]);

  const discover = useCallback(async (signal: AbortSignal) => {
    let after: string | undefined;
    do {
      const { data } = await retryRead(() => knowledgeApi.listUploadJobs(after, signal), signal);
      if (signal.aborted) return;
      data.data.jobs.forEach((job) => watch(fromJob(job)));
      after = data.data.next_cursor ?? undefined;
    } while (after);
    await refreshDocuments(signal);
  }, [watch, refreshDocuments]);

  useEffect(() => {
    const discovery = new AbortController();
    const controllers = running.current;
    void discover(discovery.signal).catch(() => {});
    return () => {
      discovery.abort();
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
    };
  }, [discover]);

  const uploadFile = useCallback(async (file: File) => {
    const id = crypto.randomUUID();
    const controller = new AbortController();
    running.current.set(id, controller);
    const row: UploadProgress = { id, filename: file.name, progress: 0, stage: 'preparing', status: 'uploading' };
    show(row);
    try {
      const response = await knowledgeApi.upload(file, controller.signal);
      const jobId = await readUploadReceipt(response, controller.signal);
      if (controller.signal.aborted) return;
      if (jobId) {
        watch({ ...row, id: jobId, stage: 'queued' }, id);
      } else {
        show({ ...row, progress: 100, status: 'completed', stage: 'complete' });
        await refreshDocuments(controller.signal);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        show({ ...row, status: 'error', message: error instanceof Error ? error.message : '上传失败' });
        // The server might have accepted the upload before the connection closed.
        if (error instanceof TypeError || (error instanceof Error && error.message === '上传连接中断')) {
          await discover(controller.signal).catch(() => {});
        }
      }
    } finally {
      running.current.delete(id);
    }
  }, [discover, refreshDocuments, show, watch]);

  return { uploads, uploadFile, clearCompleted: () => setUploads((previous) => previous.filter(
    (item) => item.status === 'uploading',
  )) };
}
