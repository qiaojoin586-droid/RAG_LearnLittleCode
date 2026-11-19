import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import client from '../../api/client';
import { endpoints } from '../../api/endpoints';
import type { ApiResponse } from '../../types/api';

interface Approval {
  operation_id: string;
  tool_name: 'create_note_tool' | 'update_note_tool' | 'mark_reviewed_tool' | 'send_email';
  preview_hash: string;
  arguments: { title?: string; content?: string; tags?: string[]; category?: string | null;
    to?: string; subject?: string; body?: string; format?: 'md' | 'pdf' | 'text' };
  target: { id?: string | number; title?: string; category?: { name: string | null } };
  expires_at: string;
}

type Decision = 'approve' | 'reject';
type EmailStatus = 'dispatching' | 'accepted' | 'unknown' | 'failed' | 'received';
type DecisionStatus = 'executed' | 'rejected' | 'expired' | 'conflict' | EmailStatus;

interface EmailOperation {
  operation_id: string;
  status: EmailStatus;
  result: { to: string; subject: string; message_id: string };
}

export default function ToolApprovals({ sessionId, streaming }: { sessionId: string; streaming: boolean }) {
  const { t, i18n } = useTranslation();
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [emails, setEmails] = useState<EmailOperation[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<DecisionStatus | null>(null);
  const [error, setError] = useState<'load' | 'decision' | null>(null);
  const [refresh, setRefresh] = useState(0);
  const unconfirmed = error === 'decision';
  const poll = streaming || emails.some((item) => item.status === 'dispatching');

  useEffect(() => {
    if (busy || unconfirmed) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function load() {
      const options = { signal: controller.signal };
      const [pending, results] = await Promise.allSettled([
        client.get<ApiResponse<Approval[]>>(endpoints.chat.approvals(sessionId), options),
        client.get<ApiResponse<EmailOperation[]>>(endpoints.chat.emailOperations(sessionId), options),
      ]);
      if (controller.signal.aborted) return;
      if (pending.status === 'fulfilled') setApprovals(pending.value.data.data);
      if (results.status === 'fulfilled') setEmails(results.value.data.data);
      setError(pending.status === 'rejected' || results.status === 'rejected' ? 'load' : null);
      if (poll) timer = window.setTimeout(load, 2500);
    }
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [sessionId, poll, busy, refresh, unconfirmed]);

  async function decide(item: Approval, decision: Decision) {
    setBusy(item.operation_id);
    setError(null);
    setNotice(null);
    try {
      const response = await client.post<ApiResponse<{ status: DecisionStatus }>>(
        endpoints.chat.decideApproval(item.operation_id), { decision, preview_hash: item.preview_hash },
      );
      setNotice(response.data.data.status);
      setApprovals((items) => items.filter((current) => current.operation_id !== item.operation_id));
    } catch {
      setError('decision');
    } finally {
      setBusy(null);
    }
  }

  async function recordReceipt(operation: EmailOperation) {
    setBusy(operation.operation_id);
    setError(null);
    try {
      await client.post(endpoints.chat.emailReceived(operation.operation_id));
      setNotice('received');
      setEmails((items) => items.map((item) => item.operation_id === operation.operation_id
        ? { ...item, status: 'received' } : item));
    } catch {
      setError('decision');
    } finally {
      setBusy(null);
    }
  }

  const unknownCount = emails.filter((item) => item.status === 'unknown').length;
  if (approvals.length === 0 && emails.length === 0 && !notice && !error) return null;

  return (
    <section className="max-h-[40dvh] shrink-0 overflow-y-auto border-t border-[var(--color-assistant-border)] px-4 py-1 text-sm text-slate-900 dark:text-slate-100"
      aria-label={t('chat.approvals')}>
      {notice && <p className="my-2" role="status">{t(`chat.approval.${notice}`)}</p>}
      {error && (
        <div className="my-1 flex items-center justify-between gap-2">
          <p role="alert">{t(`chat.approval.${error}Error`)}</p>
          <button type="button" className="min-h-11 shrink-0 rounded-md px-2 underline" onClick={() => {
            setError(null);
            setRefresh((value) => value + 1);
          }}>{t('chat.approval.refresh')}</button>
        </div>
      )}
      {approvals.length > 0 && (
        <details>
          <summary className="flex min-h-11 cursor-pointer items-center gap-2 font-medium focus-visible:ring-2 focus-visible:ring-sky-500">
            {t('chat.approvals')} <span className="tabular-nums">{approvals.length}</span>
          </summary>
          <div className="space-y-2 pb-2">
            {approvals.map((item) => {
              const args = item.arguments;
              const fields = item.tool_name === 'send_email' ? [
                ['recipient', args.to], ['subject', args.subject], ['content', args.body],
                ['format', args.format ? t(`chat.approval.format.${args.format}`) : undefined],
              ] as const : [
                ['title', args.title],
                ['content', args.content],
                ['tags', args.tags?.join(', ')],
                ['category', item.target.category ? (item.target.category.name ?? '') : undefined],
              ] as const;
              return (
                <article key={item.operation_id} className="rounded-lg border border-[var(--color-assistant-border)] p-3">
                  <h4 className="break-words font-semibold">{t(`chat.approval.${item.tool_name}`)}</h4>
                  {item.target.title && <p className="mt-1 break-words">{item.target.title} · {item.target.id}</p>}
                  <p className="mt-1 text-slate-600 dark:text-slate-300">
                    {t('chat.approval.expires', { time: new Date(item.expires_at).toLocaleTimeString(i18n.language, {
                      hour: '2-digit', minute: '2-digit',
                    }) })}
                  </p>
                  {fields.some(([, value]) => value !== undefined) && (
                    <dl className="mt-2 space-y-2">
                      {fields.map(([field, value]) => value !== undefined && (
                        <div key={field}>
                          <dt className="font-medium">{t(`chat.approval.${field}`)}</dt>
                          <dd className="whitespace-pre-wrap break-words">{value || t('chat.approval.empty')}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button type="button" onClick={() => void decide(item, 'approve')} disabled={busy !== null}
                      className="min-h-11 rounded-md bg-blue-700 px-3 font-medium text-white hover:bg-blue-800 focus-visible:ring-2 focus-visible:ring-sky-500 disabled:opacity-60">
                      {t(busy === item.operation_id ? 'chat.approval.processing'
                        : item.tool_name === 'send_email' ? 'chat.approval.send' : 'chat.approval.approve')}
                    </button>
                    <button type="button" onClick={() => void decide(item, 'reject')} disabled={busy !== null}
                      className="min-h-11 rounded-md border border-[var(--color-assistant-border)] px-3 font-medium hover:underline focus-visible:ring-2 focus-visible:ring-sky-500 disabled:opacity-60">
                      {t('chat.approval.reject')}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </details>
      )}
      {emails.length > 0 && (
        <details>
          <summary className="flex min-h-11 cursor-pointer items-center gap-2 font-medium focus-visible:ring-2 focus-visible:ring-sky-500">
            {t('chat.emailOperations')}{unknownCount > 0 && <span>{t('chat.approval.unknownCount', { count: unknownCount })}</span>}
          </summary>
          <div className="space-y-2 pb-2">
            {emails.map((item) => (
              <article key={item.operation_id} className="space-y-1 rounded-lg border border-[var(--color-assistant-border)] p-3">
                <h4 className="break-words font-semibold">{item.result.subject}</h4>
                <p className="break-all">{item.result.to}</p>
                <p>{t(`chat.approval.${item.status}`)}</p>
                <p className="break-all">Message-ID <code>{item.result.message_id}</code></p>
                {(item.status === 'accepted' || item.status === 'unknown') && (
                  <button type="button" disabled={busy !== null} onClick={() => void recordReceipt(item)}
                    className="min-h-11 rounded-md border border-[var(--color-assistant-border)] px-3 font-medium hover:underline focus-visible:ring-2 focus-visible:ring-sky-500 disabled:opacity-60">
                    {t(busy === item.operation_id ? 'chat.approval.processing' : 'chat.approval.markReceived')}
                  </button>
                )}
              </article>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
