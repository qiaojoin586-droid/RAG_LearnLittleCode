import { useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { configError, modelConfigApi, type CredentialImpact, type CredentialVersion, type RoleDefinition } from '../api/modelConfig';

interface Props {
  canRevoke: boolean; roles: RoleDefinition[]; askProof: () => Promise<string | null>; onRevoked: (id: string) => void;
}
const statusName = { candidate: '草稿候选', active: '已发布', retired: '已退役', revoked: '已撤销' };

export default function ModelConfigCredentials({ canRevoke, roles, askProof, onRevoked }: Props) {
  const [items, setItems] = useState<CredentialVersion[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [impact, setImpact] = useState<CredentialImpact | null>(null);
  const [reason, setReason] = useState('suspected_leak');
  const [confirmed, setConfirmed] = useState(false);
  const alive = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const replay = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; controller.current?.abort(); };
  }, []);
  const roleName = (key: string) => roles.find(role => role.role_key === key)?.display_name ?? key;
  async function read(more = false) {
    controller.current?.abort(); const request = new AbortController(); controller.current = request;
    setBusy(true); setError('');
    try {
      const result = await modelConfigApi.credentials(more ? cursor ?? undefined : undefined, request.signal);
      if (!request.signal.aborted && alive.current) {
        setItems(values => more ? [...values, ...result.items] : result.items); setCursor(result.next_cursor ?? null); setLoaded(true);
      }
    } catch (failure) { if (!request.signal.aborted && alive.current) setError(configError(failure).message); }
    finally { if (!request.signal.aborted && alive.current) setBusy(false); }
  }
  async function inspect(id: string) {
    controller.current?.abort(); const request = new AbortController(); controller.current = request;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await modelConfigApi.credentialImpact(id, request.signal);
      if (!request.signal.aborted && alive.current) { setImpact(result); setConfirmed(false); setReason('suspected_leak'); }
    } catch (failure) { if (!request.signal.aborted && alive.current) setError(configError(failure).message); }
    finally { if (!request.signal.aborted && alive.current) setBusy(false); }
  }
  async function revoke() {
    if (!impact || !confirmed || !canRevoke) return;
    const id = impact.credential.id; const selectedReason = reason;
    setImpact(null); setBusy(true); setError('');
    try {
      const proof = await askProof(); if (!proof || !alive.current) return;
      const signature = JSON.stringify({ id, reason: selectedReason });
      if (replay.current?.signature !== signature) replay.current = { signature, key: crypto.randomUUID() };
      const result = await modelConfigApi.revokeCredential(id, selectedReason, replay.current.key, proof);
      if (!alive.current) return;
      setItems(values => values.map(item => item.id === id ? { ...item, status: 'revoked', revocation_epoch: result.revocation_epoch } : item));
      setNotice('此凭据版本已撤销：后续调用许可已阻止，正在进行的连接已请求停止。');
      replay.current = null; onRevoked(id);
    } catch (failure) { if (alive.current) setError(configError(failure).message); }
    finally { if (alive.current) setBusy(false); }
  }
  return <details className="mc-policy"><summary>凭据版本与紧急撤销</summary>
    <p className="mc-muted">撤销作用于本系统的一份凭据版本，会影响所有引用它的角色和历史配置。其他版本即使保存了相同的 Key，也不会自动撤销。</p>
    <div className="mc-actions"><button disabled={busy} onClick={() => void read()}>{busy ? '正在处理…' : '读取凭据版本'}</button>
      {cursor && <button disabled={busy} onClick={() => void read(true)}>更多凭据版本</button>}</div>
    {loaded && !items.length && <p role="status">尚未保存需要认证的凭据版本。无认证 Ollama 不会生成 Key 版本。</p>}
    {error && <p role="alert" className="mc-error">{error}</p>}
    {notice && <p role="status" className="mc-success">{notice}</p>}
    <div className="mc-baseline-grid">{items.map(item => <article className="mc-test-result" key={item.id}>
      <h3>{roleName(item.credential_key)} · 凭据 v{item.credential_version}</h3><p>{statusName[item.status]} · {item.masked_suffix ? `••••${item.masked_suffix}` : '不显示尾号'}</p>
      <p>{item.allowed_origin}</p><button disabled={busy} onClick={() => void inspect(item.id)}>
        {canRevoke && item.status !== 'revoked' ? '查看影响并撤销' : '查看影响'}</button>
    </article>)}</div>
    <Dialog.Root open={Boolean(impact)} onOpenChange={open => { if (!open) setImpact(null); }}><Dialog.Portal>
      <Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog">
        <Dialog.Title>{canRevoke && impact?.credential.status !== 'revoked' ? '确认凭据撤销范围' : '凭据版本影响范围'}</Dialog.Title>
        <Dialog.Description>撤销不能恢复。它将阻止本系统继续使用此版本，并尽力停止进行中的连接；无法撤回供应商已受理的内容，也不会替你撤销供应商侧的真实 Key。</Dialog.Description>
        {impact && <><p>{roleName(impact.credential.credential_key)} · 凭据 v{impact.credential.credential_version} · {impact.credential.masked_suffix ? `••••${impact.credential.masked_suffix}` : '不显示尾号'}<br />{impact.credential.allowed_origin}</p>
          <p>当前启用角色：{impact.active_role_keys.map(roleName).join('、') || '无'}<br />可能影响的动态跟随角色：{impact.possible_dynamic_role_keys.map(roleName).join('、') || '无'}<br />
            当前索引：{impact.active_index_keys.join('、') || '无'}<br />历史发布：{impact.historical_release_count}；关联草稿（含历史引用）：{impact.referenced_draft_count}</p>
          {impact.credential.status === 'revoked' ? <p>此版本已经撤销。需要恢复调用时，请保存新 Key 生成新的凭据版本并重新测试、发布。</p> : canRevoke && <>
            <label>撤销原因<select value={reason} onChange={event => setReason(event.target.value)}>
              <option value="suspected_leak">疑似密钥泄露</option><option value="provider_disabled">供应商已停用此 Key</option><option value="administrator_action">管理员主动停用</option>
            </select></label>
            <label className="mc-checkbox"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认撤销以上凭据版本的所有引用</label>
          </>}
        </>}
        <div className="mc-actions"><button onClick={() => setImpact(null)}>关闭</button>
          {canRevoke && impact?.credential.status !== 'revoked' && <button className="mc-danger" disabled={!confirmed || busy} onClick={() => void revoke()}>确认范围并验证身份</button>}</div>
      </Dialog.Content></Dialog.Portal></Dialog.Root>
  </details>;
}
