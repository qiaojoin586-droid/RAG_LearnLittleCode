import { useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import ReactMarkdown from 'react-markdown';
import { Download, FileText, History, RefreshCw, Save, Sparkles, Trash2 } from 'lucide-react';
import { memoryApi, memoryError, type MemoryDocument, type MemoryJob, type MemoryRevision } from '../api/memory';
import { useUserStore } from '../stores/useUserStore';
import './ModelConfig.css';
import './Memory.css';

const jobNames: Record<string, string> = { queued: '等待提取', running: '提取中', proposed: '有待确认建议',
  applied: '已更新', no_change: '没有新的稳定信息', failed: '提取失败', obsolete: '基线已更新', cancelled: '已取消', skipped_limit: '队列已满，已跳过' };
const active = (jobs: MemoryJob[]) => jobs.some(job => job.status === 'queued' || job.status === 'running');
function Markdown({ content }: { content: string }) {
  return <div className="memory-preview"><ReactMarkdown skipHtml components={{
    img: () => <span>［外部图片未自动加载］</span>, a: ({ children }) => <span>{children}</span>,
  }}>{content}</ReactMarkdown></div>;
}

function PersonalMemory() {
  const [document, setDocument] = useState<MemoryDocument | null>(null);
  const [content, setContent] = useState('');
  const [preview, setPreview] = useState(false);
  const [revisions, setRevisions] = useState<MemoryRevision[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [jobs, setJobs] = useState<MemoryJob[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [history, setHistory] = useState<MemoryRevision | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [purge, setPurge] = useState(false);
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const mounted = useRef(true);
  const dirtyRef = useRef(false);
  const dirty = document !== null && content !== (document.content_md ?? '');
  dirtyRef.current = dirty;
  function key(value: unknown) { const signature = JSON.stringify(value); if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() }; return pending.current.key; }
  function update(value: MemoryDocument) { if (!mounted.current) return; setDocument(value); setContent(value.content_md ?? ''); pending.current = null; setNotice('已保存，下一轮 AI 提问生效；进行中的请求保持原版本。'); }
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    Promise.all([memoryApi.get(controller.signal), memoryApi.revisions(undefined, controller.signal), memoryApi.jobs(controller.signal)])
      .then(([file, versions, tasks]) => { if (!controller.signal.aborted) { setDocument(file); setContent(file.content_md ?? ''); setRevisions(versions.items); setCursor(versions.next_cursor ?? null); setJobs(tasks.items); } })
      .catch(value => { if (!controller.signal.aborted) setError(memoryError(value)); });
    return () => { mounted.current = false; controller.abort(); pending.current = null; };
  }, []);
  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', handler); return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);
  const polling = active(jobs);
  useEffect(() => {
    if (!polling) return;
    const controller = new AbortController();
    const timer = setInterval(async () => {
      try {
        const result = await memoryApi.jobs(controller.signal);
        if (controller.signal.aborted) return;
        setJobs(result.items);
        if (!active(result.items)) {
          if (dirtyRef.current) { setNotice('提取任务已完成；你的未保存内容已保留，请核对最新版本。'); return; }
          const value = await memoryApi.get(controller.signal);
          if (!controller.signal.aborted && !dirtyRef.current) { setDocument(value); setContent(value.content_md ?? ''); }
        }
      } catch (value) { if (!controller.signal.aborted) setError(memoryError(value)); }
    }, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [polling]);
  async function refresh() {
    if (dirty && !window.confirm('有未保存内容，确定放弃并重新读取服务器版本吗？')) return;
    setBusy(true); setError('');
    try { const [value, versions, tasks] = await Promise.all([memoryApi.get(), memoryApi.revisions(), memoryApi.jobs()]);
      update(value); setRevisions(versions.items); setCursor(versions.next_cursor ?? null); setJobs(tasks.items); setNotice('已读取服务器版本。');
    } catch (value) { setError(memoryError(value)); } finally { setBusy(false); }
  }
  async function perform(action: string, operation: (key: string) => Promise<MemoryDocument>, signature: unknown) {
    setBusy(true); setError(''); setNotice('');
    try { const value = await operation(key({ action, version: document?.head_version, signature })); update(value);
      if (!mounted.current) return;
      if (action === 'clear') { setJobs([]); setHistory(null); setRevisions([]); setCursor(null); }
      // The write has committed. A failed refresh must not report it as failed.
      const [versions, tasks] = await Promise.allSettled([memoryApi.revisions(), memoryApi.jobs()]);
      if (mounted.current) {
        if (versions.status === 'fulfilled') { setRevisions(versions.value.items); setCursor(versions.value.next_cursor ?? null); }
        if (tasks.status === 'fulfilled') setJobs(tasks.value.items);
        if (versions.status === 'rejected' || tasks.status === 'rejected') setNotice('已保存；历史或任务状态暂未刷新，可稍后重新读取。');
      }
    } catch (value) { if (mounted.current) setError(memoryError(value)); } finally { if (mounted.current) setBusy(false); }
  }
  async function generate() {
    if (!document) return; setBusy(true); setError('');
    try { const result = await memoryApi.generate(document, key({ action: 'generate', version: document.head_version }));
      if (mounted.current) { setJobs(items => [result, ...items.filter(item => item.id !== result.id)]); pending.current = null; setNotice(jobNames[result.status] ?? result.status); }
    } catch (value) { setError(memoryError(value)); } finally { setBusy(false); }
  }
  async function download() {
    setBusy(true); setError('');
    try { const value = await memoryApi.download(); const url = URL.createObjectURL(value); const link = window.document.createElement('a');
      link.href = url; link.download = 'memory.md'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (value) { setError(memoryError(value)); } finally { setBusy(false); }
  }
  async function more() {
    setBusy(true);
    try { const page = await memoryApi.revisions(cursor ?? undefined); setRevisions(items => [...items, ...page.items]); setCursor(page.next_cursor ?? null); }
    catch (value) { setError(memoryError(value)); } finally { setBusy(false); }
  }
  const byteCount = new TextEncoder().encode(content).length;
  return <main className="model-config memory-page"><header className="mc-page-heading"><div><p className="mc-eyebrow">PERSONAL MEMORY</p>
    <h1><FileText size={26} aria-hidden="true" />记忆文件</h1><p className="mc-muted">你自己的 memory.md，可以随时编辑、停用或清空。</p></div>
    <div className="mc-actions"><button disabled={busy} onClick={() => void refresh()}><RefreshCw size={16} aria-hidden="true" />重新读取</button>
      <button disabled={!document?.exists || busy} onClick={() => void download()}><Download size={16} aria-hidden="true" />下载</button></div></header>
    <div className="mc-info">启用后，选定的文件片段会随提问发送给当前配置的模型服务。请勿保存 API Key、密码或验证码。个人记忆不能覆盖系统安全规则和工具授权。</div>
    {error && <p role="alert" className="mc-error">{error}</p>}{notice && <p role="status" className="mc-success">{notice}</p>}
    {!document ? <p role="status">{error ? '暂时无法读取文件。' : '正在读取文件…'}</p> : <>
      <section className="mc-editor"><div className="mc-role-heading"><div><h2>memory.md</h2><p className="mc-muted">{document.exists ? `修订 ${document.revision_number} · ${document.settings.management_mode === 'manual' ? '手工管理，AI 只给建议' : '自动管理'}` : '尚未创建；首次使用主 AI 助手时自动建立，也可先填写并保存。'}</p></div>
        <span className="mc-badge">{document.settings.enabled ? '已启用' : '已停用'}</span></div>
        <fieldset disabled={busy || dirty} className="memory-settings"><label className="mc-checkbox"><input type="checkbox" checked={document.settings.enabled}
          onChange={e => void perform('settings', requestKey => memoryApi.settings(document, { enabled: e.target.checked }, requestKey), { enabled: e.target.checked })} />在 AI 助手中使用</label>
          <label className="mc-checkbox"><input type="checkbox" checked={document.settings.auto_extract}
            onChange={e => void perform('settings', requestKey => memoryApi.settings(document, { auto_extract: e.target.checked }, requestKey), { auto_extract: e.target.checked })} />允许自动提取稳定信息</label>
          <label>管理模式<select value={document.settings.management_mode} onChange={e => {
            const mode = e.target.value as 'auto' | 'manual'; if (mode === 'auto' && !window.confirm('切回自动管理后，AI 可以更新全文；仍只从你自己的已授权消息中提取。确定继续吗？')) return;
            void perform('settings', requestKey => memoryApi.settings(document, { management_mode: mode }, requestKey), { management_mode: mode });
          }}><option value="manual">手工管理：AI 仅提出建议</option><option value="auto">自动管理：允许 AI 更新全文</option></select></label></fieldset>
        {!document.extraction_available && <p className="mc-muted">个人记忆提取角色尚未发布启用；文件仍可手工编辑并用于提问。</p>}
        <div className="mc-actions memory-editor-tabs"><div className="mc-actions"><button aria-pressed={!preview} onClick={() => setPreview(false)}>编辑</button><button aria-pressed={preview} onClick={() => setPreview(true)}>预览</button></div>
          <span className="mc-muted">{byteCount} / 32768 字节{dirty ? ' · 未保存' : ''}</span></div>
        {preview ? <Markdown content={content || '暂无内容。'} /> : <label>Markdown 正文<textarea className="memory-textarea" rows={18} value={content} disabled={busy}
          onChange={e => setContent(e.target.value)} placeholder="例如：我偏好中文回答；先给结论，再解释原因。" /></label>}
        <p className="mc-muted">正文可全文修改；过长时按预算选取片段，并保留本轮选定版本。手工保存会进入手工管理，不会擅自重新启用文件。</p>
        <div className="mc-actions"><button className="mc-primary" disabled={busy || !dirty || byteCount > 32768}
          onClick={() => void perform('save', requestKey => memoryApi.save(document, content, requestKey), content)}><Save size={16} aria-hidden="true" />保存，下一轮生效</button>
          <button disabled={busy || dirty} onClick={() => setClearOpen(true)}><Trash2 size={16} aria-hidden="true" />清空并停用</button></div>
      </section>
      <section className="mc-editor memory-section"><div className="mc-actions"><h2><Sparkles size={18} aria-hidden="true" />AI 建议与提取状态</h2>
        <button disabled={busy || dirty || !document.extraction_available || !document.settings.auto_extract || !document.settings.enabled || polling} onClick={() => void generate()}>从最近成功对话提取</button></div>
        <p className="mc-muted">只读取你最近一条符合条件的用户原文与文件基线，不提取 AI 回答或工具输出。</p>
        {jobs.length === 0 && <p className="mc-muted">还没有提取记录。</p>}
        {jobs.map(job => <article className="memory-job" key={job.id}><div className="mc-actions"><strong>{jobNames[job.status] ?? job.status}</strong><span className="mc-muted">来源消息 #{job.source_message_id} · 第 {job.generation_number} 次</span></div>
          {job.error_reason && <p className="mc-muted">原因：{job.error_reason}</p>}
          {job.status === 'proposed' && <><div className="memory-diff"><div><h3>当前文件</h3><pre>{document.content_md}</pre></div><div><h3>建议全文</h3><pre>{job.proposed_content_md}</pre></div></div>
            <div className="mc-actions"><button disabled={busy || dirty} onClick={() => void perform('apply', requestKey => memoryApi.apply(document, job.id, requestKey), job.id)}>采用建议</button>
              <button disabled={busy || dirty} onClick={() => void perform('dismiss', requestKey => memoryApi.dismiss(document, job.id, requestKey), job.id)}>忽略建议</button></div></>}
        </article>)}
      </section>
      <section className="mc-editor memory-section"><h2><History size={18} aria-hidden="true" />历史版本</h2>
        {revisions.length === 0 && <p className="mc-muted">创建文件后，这里会保留版本记录。</p>}
        {revisions.map(revision => <div className="memory-revision" key={revision.id}><span>修订 {revision.revision_number} · {revision.source_kind} · {revision.created_at.slice(0, 10)}</span><div className="mc-actions">
          <button disabled={busy} onClick={async () => { try { setHistory(await memoryApi.revision(revision.id)); } catch (value) { setError(memoryError(value)); } }}>查看</button>
          <button disabled={busy || dirty} onClick={() => { if (window.confirm(`将修订 ${revision.revision_number} 恢复为一个新版本？文件的启停状态保持不变。`)) void perform('restore', requestKey => memoryApi.restore(document, revision.id, requestKey), revision.id); }}>恢复为新版本</button></div></div>)}
        {cursor && <button disabled={busy} onClick={() => void more()}>加载更多版本</button>}
      </section>
    </>}
    <Dialog.Root open={clearOpen} onOpenChange={setClearOpen}><Dialog.Portal><Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog">
      <Dialog.Title>清空并停用记忆文件</Dialog.Title><Dialog.Description>清空会停止后续注入和自动提取，使旧任务失效。正在发送的请求无法从供应商处撤回。</Dialog.Description>
      <label className="memory-clear-checkbox"><input type="checkbox" checked={purge} onChange={e => setPurge(e.target.checked)} />同时彻底删除历史、来源和候选正文（不可恢复）</label>
      <div className="mc-actions"><button onClick={() => setClearOpen(false)}>取消</button><button onClick={() => { setClearOpen(false); if (document) void perform('clear', requestKey => memoryApi.clear(document, purge, requestKey), purge); }}>确认清空并停用</button></div>
    </Dialog.Content></Dialog.Portal></Dialog.Root>
    <Dialog.Root open={Boolean(history)} onOpenChange={open => { if (!open) setHistory(null); }}><Dialog.Portal><Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog memory-history-dialog">
      <Dialog.Title>历史修订 {history?.revision_number}</Dialog.Title><Dialog.Description>只读历史；恢复会另建新版本。</Dialog.Description><Markdown content={history?.content_md ?? ''} />
      <button onClick={() => setHistory(null)}>关闭</button></Dialog.Content></Dialog.Portal></Dialog.Root>
  </main>;
}

export default function Memory() {
  // A user switch must destroy plaintext editor state before loading another owner.
  const owner = useUserStore(state => state.userInfo?.username ?? '');
  return <PersonalMemory key={owner} />;
}
