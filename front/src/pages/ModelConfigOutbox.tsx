import { useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { configError, modelConfigApi, type OutboxEvent, type OutboxStatus } from '../api/modelConfig';

const names: Record<OutboxStatus, string> = { pending: '待处理', inflight: '处理中', succeeded: '已送达', retry: '等待重试', dead_letter: '死信' };
const typeName = (event: OutboxEvent) => event.event_type === 'credential.revoked' ? '凭据撤销通知' : '配置发布通知';
const time = (value: string) => new Date(/[zZ]|[+-]\d{2}:\d{2}$/.test(value) ? value : `${value}Z`).toLocaleString();

export default function ModelConfigOutbox({ askProof }: { askProof: () => Promise<string | null> }) {
  const [items, setItems] = useState<OutboxEvent[]>([]);
  const [filter, setFilter] = useState<OutboxStatus | ''>('dead_letter');
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<OutboxEvent | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const alive = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const replay = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; controller.current?.abort(); };
  }, []);
  async function read(more = false) {
    controller.current?.abort(); const request = new AbortController(); controller.current = request;
    setBusy(true); setError('');
    try {
      const result = await modelConfigApi.outbox(filter || undefined, more ? cursor ?? undefined : undefined, request.signal);
      if (request.signal.aborted || !alive.current) return;
      setItems(values => more ? [...values, ...result.items.filter(item => !values.some(old => old.id === item.id))] : result.items);
      setCursor(result.next_cursor ?? null); setLoaded(true);
    } catch (failure) { if (!request.signal.aborted && alive.current) setError(configError(failure).message); }
    finally { if (!request.signal.aborted && alive.current) setBusy(false); }
  }
  async function inspect(id: string) {
    controller.current?.abort(); const request = new AbortController(); controller.current = request;
    setBusy(true); setError('');
    try {
      const result = await modelConfigApi.outboxEvent(id, request.signal);
      if (!request.signal.aborted && alive.current) { setSelected(result); setConfirmed(false); }
    } catch (failure) { if (!request.signal.aborted && alive.current) setError(configError(failure).message); }
    finally { if (!request.signal.aborted && alive.current) setBusy(false); }
  }
  async function redrive() {
    if (!selected || selected.status !== 'dead_letter' || selected.redrive_event_id || !confirmed) return;
    const event = selected; setSelected(null); setBusy(true); setError('');
    try {
      const proof = await askProof(); if (!proof || !alive.current) return;
      const signature = JSON.stringify({ id: event.id, attempts: event.attempts });
      if (replay.current?.signature !== signature) replay.current = { signature, key: crypto.randomUUID() };
      const result = await modelConfigApi.redriveOutbox(event.id, event.attempts, replay.current.key, proof);
      if (!alive.current) return;
      setItems(values => values.map(item => item.id === event.id ? { ...item, redrive_event_id: result.event_id } : item));
      setNotice('补投事件已创建，等待后台处理。原死信保留，请查看补投状态确认送达结果。'); replay.current = null;
    } catch (failure) { if (alive.current) setError(configError(failure).message); }
    finally { if (alive.current) setBusy(false); }
  }
  return <details className="mc-policy"><summary>配置通知与失败处理</summary>
    <p className="mc-muted">通知失败不会撤回已提交的发布或撤销。每个事件最多尝试 5 次；死信可人工补投，原记录保留。</p>
    <div className="mc-actions"><label>通知状态<select aria-label="通知状态" value={filter} disabled={busy} onChange={event => {
      setFilter(event.target.value as OutboxStatus | ''); setItems([]); setCursor(null); setLoaded(false); setNotice('');
    }}><option value="">全部状态</option>{Object.entries(names).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</select></label>
      <button disabled={busy} onClick={() => void read()}>{busy ? '正在处理通知…' : '刷新通知'}</button>
      {cursor && <button disabled={busy} onClick={() => void read(true)}>更多通知</button>}</div>
    {loaded && !items.length && <p role="status">当前筛选下没有配置通知。</p>}
    {error && <p role="alert" className="mc-error">{error}</p>}
    {notice && <p role="status" className="mc-success">{notice}</p>}
    <div className="mc-baseline-grid">{items.map(item => <article className="mc-test-result" key={item.id}>
      <h3>{typeName(item)} · {names[item.status]}</h3><p>已尝试 {item.attempts} / 5 次 · {time(item.created_at)}</p>
      <p>事件 {item.id}<br />目标 {item.aggregate_id} · 版本 {item.aggregate_version}</p>
      {item.status === 'retry' && <p>下次尝试：{time(item.next_attempt_at)}</p>}
      {item.error_code && <p>通知处理未完成，请检查控制存储、后台任务及管理租约。</p>}
      <div className="mc-actions"><button disabled={busy} onClick={() => void inspect(item.id)}>查看通知</button>
        {item.status === 'dead_letter' && !item.redrive_event_id && <button disabled={busy} onClick={() => void inspect(item.id)}>确认并补投</button>}
        {item.redrive_event_id && <button disabled={busy} onClick={() => void inspect(item.redrive_event_id!)}>查看补投状态</button>}</div>
    </article>)}</div>
    <Dialog.Root open={Boolean(selected)} onOpenChange={open => { if (!open) setSelected(null); }}><Dialog.Portal>
      <Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog">
        <Dialog.Title>配置通知详情</Dialog.Title>
        <Dialog.Description>补投会创建新通知，不会重新发布配置、恢复凭据或重新调用模型。通知可能重复送达，处理方需保持幂等。</Dialog.Description>
        {selected && <><p>{typeName(selected)} · {names[selected.status]} · 已尝试 {selected.attempts} / 5 次</p>
          <p>事件 {selected.id}<br />目标 {selected.aggregate_id} · 版本 {selected.aggregate_version}</p>
          {selected.status === 'dead_letter' && !selected.redrive_event_id && <label className="mc-checkbox">
            <input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认补投此通知，并保留原失败记录</label>}
          {selected.redrive_event_id && <p>此死信已创建补投事件，请返回列表查看补投状态。</p>}</>}
        <div className="mc-actions"><button onClick={() => setSelected(null)}>关闭</button>
          {selected?.redrive_event_id && <button disabled={busy} onClick={() => void inspect(selected.redrive_event_id!)}>查看该次补投状态</button>}
          {selected?.status === 'dead_letter' && !selected.redrive_event_id && <button disabled={!confirmed || busy} onClick={() => void redrive()}>确认补投并验证身份</button>}</div>
      </Dialog.Content></Dialog.Portal></Dialog.Root>
  </details>;
}
