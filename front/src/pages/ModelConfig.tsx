import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { CheckCircle2, ChevronRight, Cpu, FlaskConical, LockKeyhole, Plus, RefreshCw, Save } from 'lucide-react';
import { configError, modelConfigApi, type Binding, type CredentialAction, type Draft, type Operation,
  type Policy, type PolicyInput, type ProfileInput, type RoleDefinition, type RoleKey, type Summary, type Validation, type Release,
  type IndexCatchUp, type RebaseConflict, type RebaseResolution } from '../api/modelConfig';
import './ModelConfig.css';
import ModelConfigEmbedding from './ModelConfigEmbedding';
import ModelConfigCredentials from './ModelConfigCredentials';
import ModelConfigOutbox from './ModelConfigOutbox';

type DraftItem = Pick<Draft, 'id' | 'draft_version' | 'status'>;
type AskProof = () => Promise<string | null>;
const statusName: Record<string, string> = { editing: '草稿', ready: '已验证草稿', published: '已发布',
  abandoned: '已放弃', expired: '已过期', running: '测试中', pending: '等待测试', passed: '测试通过', failed: '测试未通过', unverified: '尚未验证', cancelled: '测试已取消' };
const capabilityName: Record<string, string> = { text_stream: '流式文本', structured_output: '结构化输出',
  tool_roundtrip: '工具往返', image_input: '图像输入', thinking_control: '思考控制', embedding: '向量批次' };
const catchUpStatus: Record<string, string> = {
  queued: '追平任务已排队', building: '正在读取历史集合', catching_up: '正在同步新增、编辑和删除',
  ready_to_switch: '历史索引已追平，可以提交恢复', failed: '历史索引追平失败', switched: '历史索引已切换',
};

function RebaseSummary({ value }: { value: Record<string, unknown> | null }) {
  if (!value) return <p>此版本中不存在该项。</p>;
  if ('index_version_id' in value) return <p>索引版本：{String(value.index_version_id)}；查询配置：{String(value.query_profile_revision_id)}</p>;
  const selection = value.selection === 'follow_execution' ? '跟随本轮执行模型'
    : value.selection === 'follow_role' ? `跟随 ${String(value.source_role)}` : String(value.model ?? '未配置独立模型');
  return <div className="mc-merge-summary"><p>{value.enabled ? '已启用' : '未启用'} · {selection}</p>
    {value.model_revision != null && <p>连接配置版本：{String(value.model_revision)}</p>}
    <p>提示词版本：{value.prompt_revision == null ? '未配置' : String(value.prompt_revision)}；
      凭据标识：{value.credential_revision_id ? String(value.credential_revision_id).slice(0, 8) : '无'}</p></div>;
}

function useReplayKey() {
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const key = useCallback((body: unknown) => {
    const signature = JSON.stringify(body);
    if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
    return pending.current.key;
  }, []);
  const clear = useCallback(() => { pending.current = null; }, []);
  return { key, clear };
}

function ReauthDialog({ resolver, close }: { resolver: ((proof: string | null) => void) | null; close: () => void }) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError('');
    const value = password; setPassword('');
    try { const result = await modelConfigApi.reauth(value); resolver?.(result.proof); close(); }
    catch (failure) { setError(configError(failure).message); }
    finally { setBusy(false); }
  }
  function dismiss() { if (busy) return; setPassword(''); resolver?.(null); close(); }
  return <Dialog.Root open={Boolean(resolver)} onOpenChange={open => { if (!open) dismiss(); }}>
    <Dialog.Portal><Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog" onEscapeKeyDown={e => { if (busy) e.preventDefault(); }}>
      <Dialog.Title>验证管理员身份</Dialog.Title>
      <Dialog.Description>执行敏感配置操作前，请输入当前账号密码。密码和认证证明仅用于本次操作。</Dialog.Description>
      <form onSubmit={submit} className="mc-stack">
        <label>当前账号密码<input autoComplete="current-password" type="password" value={password} onChange={e => setPassword(e.target.value)} required disabled={busy} /></label>
        {error && <p role="alert" className="mc-error">{error}</p>}
        <div className="mc-actions"><button type="button" onClick={dismiss} disabled={busy}>取消</button><button className="mc-primary" disabled={busy || !password}>{busy ? '正在验证…' : '验证并继续'}</button></div>
      </form>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>;
}

function PolicyEditor({ askProof, onCreated }: { askProof: AskProof; onCreated: (policy: Policy) => void }) {
  const [local, setLocal] = useState(false);
  const [policy, setPolicy] = useState<PolicyInput>({ policy_key: '', origin: 'https://api.openai.com',
    allowed_path_prefix: '/v1', address_mode: 'public_https', connection_rules: { allowed_cidrs: [] }, environment: 'development' });
  const [cidrs, setCidrs] = useState('127.0.0.1/32, ::1/128');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const replay = useReplayKey();
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(''); setBusy(true);
    try {
      const proof = await askProof(); if (!proof) return;
      const body = { ...policy, address_mode: local ? 'trusted_private' as const : 'public_https' as const,
        connection_rules: { ...policy.connection_rules, allowed_cidrs: local ? cidrs.split(',').map(s => s.trim()).filter(Boolean) : [] } };
      const result = await modelConfigApi.createPolicy(body, replay.key(body), proof);
      onCreated(result); replay.clear(); setPolicy({ ...policy, policy_key: '' });
    } catch (failure) { setError(configError(failure).message); }
    finally { setBusy(false); }
  }
  return <details className="mc-policy"><summary>添加服务商端点策略</summary>
    <p className="mc-muted">先登记允许使用的地址，再给各角色选择。保存策略不会调用模型。Ollama 地址必须能从后端访问。</p>
    <form onSubmit={submit} className="mc-stack"><fieldset disabled={busy}>
      <label>服务类型<select value={local ? 'local' : 'cloud'} onChange={e => {
        const next = e.target.value === 'local'; setLocal(next); setPolicy({ ...policy,
          origin: next ? 'http://127.0.0.1:11434' : 'https://api.openai.com', allowed_path_prefix: next ? '/' : '/v1' });
      }}><option value="cloud">云端 HTTPS</option><option value="local">Ollama / 可信私网</option></select></label>
      <div className="mc-grid"><label>策略名称<input value={policy.policy_key} pattern="[a-z][a-z0-9_-]{0,63}" placeholder="例如 local-ollama" required onChange={e => setPolicy({ ...policy, policy_key: e.target.value })} /></label>
        <label>服务地址（不含路径）<input type="url" required value={policy.origin} onChange={e => setPolicy({ ...policy, origin: e.target.value })} /></label>
        <label>允许的路径前缀<input required value={policy.allowed_path_prefix} onChange={e => setPolicy({ ...policy, allowed_path_prefix: e.target.value })} /></label>
        {local && <label>允许的私网 CIDR<input required value={cidrs} onChange={e => setCidrs(e.target.value)} /><span>多个网段用逗号分隔；本机可使用上方默认值。</span></label>}</div>
      <label className="mc-checkbox"><input type="checkbox" checked={policy.connection_rules.allow_cross_service_fallback ?? false}
        onChange={e => setPolicy({ ...policy, connection_rules: { ...policy.connection_rules,
          allow_cross_service_fallback: e.target.checked } })} />允许跨服务备用切换</label>
      {policy.connection_rules.allow_cross_service_fallback && <p className="mc-muted">当首个服务暂时不可用时，备用服务可能接收本次请求的对话与上下文。主备策略均须启用此选项。</p>}
      {error && <p role="alert" className="mc-error">{error}</p>}
      <button type="submit" className="mc-secondary">{busy ? '正在保存…' : '验证身份并保存策略'}</button>
    </fieldset></form>
  </details>;
}

function defaultProfile(role: RoleDefinition, policy?: Policy): ProfileInput {
  return { display_name: role.display_name, model_kind: role.model_kind, adapter_type: 'openai_compatible',
    api_protocol: role.model_kind === 'embedding' ? 'openai_embeddings' : 'chat_completions', vendor: 'custom',
    base_url: policy ? policy.origin + policy.allowed_path_prefix : 'https://api.openai.com/v1', model: '',
    model_revision: null, auth_mode: 'bearer', endpoint_policy_revision_id: policy?.id ?? '', context_window: null,
    tokenizer_id: null, tokenizer_revision: null, token_counting_policy: {}, max_output_tokens: role.model_kind === 'embedding' ? null : 2048,
    connect_timeout_ms: 5000, request_timeout_ms: 30000, embedding_dimension: null, parameters: {} };
}

const unconfiguredBinding: Binding = { enabled: false, selection: 'explicit', prompt_revision_id: null,
  profile_revision_id: null, credential_revision_id: null, source_role: null, fallback: [], failure_mode: 'disabled' };

function BindingEditor({ draft, role, roles, busy, fallbackEnabled, onSave, onDirty }: { draft: Draft; role: RoleDefinition; roles: RoleDefinition[];
  busy: boolean; fallbackEnabled: boolean; onSave: (binding: Binding) => Promise<void>; onDirty: (dirty: boolean) => void }) {
  const raw = draft.payload.role_bindings_raw[role.role_key] ?? unconfiguredBinding;
  const expansion = role.role_key === 'rag_query_expand';
  const resolved = draft.resolved_static_bindings[role.role_key];
  const [binding, setBinding] = useState<Binding>({ ...raw });
  const candidates = roles.filter(r => r.model_kind === role.model_kind && r.role_key !== role.role_key && r.role_key !== 'rag_query_expand');
  const backups = candidates.filter(r => draft.resolved_static_bindings[r.role_key]?.enabled &&
    draft.resolved_static_bindings[r.role_key]?.profile_revision_id && !draft.payload.role_bindings_raw[r.role_key]?.fallback.length);
  function selection(value: Binding['selection']) {
    setBinding({ ...binding, selection: value, source_role: value === 'follow_role' ? candidates[0]?.role_key : null,
      profile_revision_id: value === 'explicit' ? resolved?.profile_revision_id ?? null : null,
      credential_revision_id: value === 'explicit' ? resolved?.credential_revision_id ?? null : null,
      fallback: value === 'follow_execution' ? [] : binding.fallback });
  }
  const changed = JSON.stringify(raw) !== JSON.stringify(binding);
  useEffect(() => { onDirty(changed); }, [changed, onDirty]);
  return <details className="mc-binding"><summary>角色绑定与失败策略</summary>
    <p className="mc-muted">先保存独立模型和提示词，再设置角色是否启用。这里的修改只影响草稿。</p>
    <fieldset disabled={busy} className="mc-stack">
      <label className="mc-checkbox"><input type="checkbox" checked={binding.enabled} onChange={e => setBinding({ ...binding, enabled: e.target.checked })} />在草稿中启用此角色</label>
      <div className="mc-grid"><label>模型选择方式<select value={binding.selection} onChange={e => selection(e.target.value as Binding['selection'])}>
        <option value="explicit">独立配置</option>{!expansion && <option value="follow_role">跟随其他角色的模型</option>}
        {role.role_key === 'synthesize' && <option value="follow_execution">跟随本轮实际执行模型</option>}</select></label>
        {binding.selection === 'follow_role' && <label>跟随角色<select value={binding.source_role ?? ''} onChange={e => setBinding({ ...binding, source_role: e.target.value as RoleKey })}>
          {candidates.map(r => <option key={r.role_key} value={r.role_key}>{r.display_name}</option>)}</select></label>}
        <label>失败时的处理<select value={binding.failure_mode} onChange={e => setBinding({ ...binding, failure_mode: e.target.value as Binding['failure_mode'] })}>
          {!expansion && <option value="error">明确返回错误</option>}<option value="disabled">{expansion ? '使用原问题检索' : '保持禁用'}</option>
          {role.role_key === 'classifier' && <option value="rule_only">仅使用规则分类</option>}
          {role.role_key === 'planner' && <option value="react_fallback">回退到 ReAct</option>}
          {role.role_key === 'session_title' && <option value="default_title">使用默认标题</option>}</select></label></div>
      {fallbackEnabled && !expansion && role.model_kind === 'chat' && binding.selection !== 'follow_execution' &&
        <label>备用模型角色<select value={binding.fallback[0] ?? ''} onChange={e =>
          setBinding({ ...binding, fallback: e.target.value ? [e.target.value as RoleKey] : [] })}>
          <option value="">无</option>
          {binding.fallback[0] && !backups.some(r => r.role_key === binding.fallback[0]) &&
            <option value={binding.fallback[0]}>当前备用角色不可用</option>}
          {backups.map(r => <option key={r.role_key} value={r.role_key}>{r.display_name}</option>)}
        </select><span>选择同服务、同认证身份的其他模型。保存后在本角色下分别测试主模型和备用模型。</span></label>}
      <button type="button" disabled={!changed || busy} onClick={() => void onSave(binding)}>保存绑定设置</button>
    </fieldset>
  </details>;
}

function RoleEditor({ draft, role, roles, policies, editable, testable, fallbackEnabled, askProof, update, dirtyChange }: {
  draft: Draft; role: RoleDefinition; roles: RoleDefinition[]; policies: Policy[]; editable: boolean; testable: boolean;
  fallbackEnabled: boolean; askProof: AskProof; update: (draft: Draft) => void; dirtyChange: (dirty: boolean) => void;
}) {
  const raw = draft.payload.role_bindings_raw[role.role_key] ?? unconfiguredBinding;
  const resolved = draft.resolved_static_bindings[role.role_key];
  const backup = fallbackEnabled ? raw.fallback[0] : undefined;
  const savedProfile = draft.profiles.find(p => p.id === resolved?.profile_revision_id);
  const savedPrompt = draft.prompts.find(p => p.id === raw.prompt_revision_id);
  const credential = draft.credential_states.find(c => c.role_key === role.role_key);
  const initialProfile = savedProfile ? Object.fromEntries(Object.keys(defaultProfile(role)).map(key => [key, savedProfile[key as keyof ProfileInput]])) as unknown as ProfileInput : defaultProfile(role, policies[0]);
  const initial = { profile: initialProfile, system: savedPrompt?.system_text ?? '', template: savedPrompt?.template_text ?? role.default_template ?? '',
    parameters: JSON.stringify(initialProfile.parameters, null, 2),
    keyAction: (initialProfile.auth_mode !== 'none' && (!credential?.has_api_key || raw.selection !== 'explicit') ? 'replace' : 'keep') as CredentialAction['action'], apiKey: '' };
  const [form, setForm] = useState(initial);
  const original = useRef(initial);
  const [bindingDirty, setBindingDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
  const [cold, setCold] = useState(false);
  const [validation, setValidation] = useState<Validation | undefined>(draft.validation_coverage.find(v => v.role_key === role.role_key));
  const replay = useReplayKey();
  const { key: testKey, clear: clearTestKey } = useReplayKey();
  const poller = useRef<AbortController | null>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(original.current);
  useEffect(() => { dirtyChange(dirty || bindingDirty); }, [dirty, bindingDirty, dirtyChange]);
  useEffect(() => () => { poller.current?.abort(); }, []);
  function profile(patch: Partial<ProfileInput>) { setForm(value => ({ ...value, profile: { ...value.profile, ...patch } })); }
  function failure(value: unknown) { const result = configError(value); setError(result.message); setConflict(result.conflict); }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setNotice(''); setConflict(false);
    try {
      const parameters: unknown = JSON.parse(form.parameters);
      if (!parameters || Array.isArray(parameters) || typeof parameters !== 'object') throw new Error('parameters');
      if (form.keyAction === 'replace' && !form.apiKey) { setError('请输入新 API Key。'); return; }
      const needsProof = form.keyAction !== 'keep';
      const proof = needsProof ? await askProof() : undefined;
      if (needsProof && !proof) return;
      const operations: Operation[] = [{ op: 'replace_profile', role_key: role.role_key,
        profile: { ...form.profile, parameters: parameters as Record<string, unknown> },
        credential: form.keyAction === 'replace' ? { action: 'replace', api_key: form.apiKey } : { action: form.keyAction },
        ...(role.role_key === 'embedding' ? { index_keys: Object.keys(draft.payload.index_bindings) } : {}) }];
      if (role.model_kind !== 'embedding') operations.push({ op: 'replace_prompt', role_key: role.role_key,
        prompt: { system_text: form.system, template_text: form.template } });
      const result = await modelConfigApi.patch(draft, operations, replay.key({ etag: draft.etag, operations }), proof ?? undefined);
      replay.clear(); setForm(value => ({ ...value, apiKey: '' })); dirtyChange(false); update(result);
    } catch (value) { if (value instanceof SyntaxError || (value instanceof Error && value.message === 'parameters')) setError('模型参数必须是 JSON 对象。'); else failure(value); }
    finally { setBusy(false); }
  }
  async function saveBinding(binding: Binding) {
    setBusy(true); setError(''); setConflict(false);
    const operations: Operation[] = [{ op: 'replace_binding', role_key: role.role_key, binding }];
    try { const result = await modelConfigApi.patch(draft, operations, replay.key({ etag: draft.etag, operations })); replay.clear(); update(result); }
    catch (value) { failure(value); } finally { setBusy(false); }
  }
  async function test(target?: RoleKey) {
    setBusy(true); setError(''); setNotice('');
    try { const key = testKey({ id: draft.id, version: draft.draft_version, role: role.role_key, target, cold });
      const result = target ? await modelConfigApi.test(draft, role.role_key, key, cold, target) :
        await modelConfigApi.test(draft, role.role_key, key, cold);
      setValidation(result); if (result.status !== 'running' && result.status !== 'pending') clearTestKey();
    } catch (value) { failure(value); setNotice('若请求超时，可用同一按钮重试以读取原测试结果；不会自动重复调用供应商。'); }
    finally { setBusy(false); }
  }
  const pendingId = validation?.status === 'running' || validation?.status === 'pending' ? validation.id : null;
  useEffect(() => {
    if (!pendingId) return;
    const controller = new AbortController(); poller.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { const next = await modelConfigApi.validation(pendingId!, controller.signal); if (!controller.signal.aborted) {
        setValidation(next); if (next.status === 'running' || next.status === 'pending') timer = setTimeout(poll, 2000); else clearTestKey();
      } } catch { if (!controller.signal.aborted) setError('测试状态读取失败，请重试读取。'); }
    }
    timer = setTimeout(poll, 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [pendingId, clearTestKey]);
  const activePolicies = policies.filter(p => p.status === 'active');
  function adapter(value: ProfileInput['adapter_type']) {
    const native = value === 'ollama_native';
    profile({ adapter_type: value, api_protocol: native ? (role.model_kind === 'embedding' ? 'ollama_embed' : 'ollama_chat')
      : value === 'dashscope_native' ? 'dashscope_embed' : role.model_kind === 'embedding' ? 'openai_embeddings' : 'chat_completions',
      vendor: native ? 'ollama' : value === 'dashscope_native' ? 'dashscope' : 'custom',
      auth_mode: native ? 'none' : value === 'dashscope_native' ? 'dashscope_key' : 'bearer' });
    setForm(value => ({ ...value, apiKey: '', keyAction: native && credential?.has_api_key ? 'clear' : 'keep' }));
  }
  return <section className="mc-editor" aria-labelledby="mc-role-title">
    <header className="mc-role-heading"><div><p className="mc-eyebrow">{role.role_key}</p><h2 id="mc-role-title">{role.display_name}</h2></div>
      <span className="mc-badge">草稿中{raw.enabled ? '已启用' : '未启用'}</span></header>
    {role.role_key === 'rag_query_expand' && <p className="mc-info">查询扩展只接收当前问题。发布后，服务端开启查询扩展时才会调用本角色。</p>}
    {raw.selection !== 'explicit' && <p className="mc-info">当前{raw.selection === 'follow_execution' ? '跟随本轮执行模型' : `跟随「${roles.find(r => r.role_key === raw.source_role)?.display_name ?? raw.source_role}」`}。下方保存将建立此角色的独立配置；提示词属于本角色。</p>}
    {bindingDirty && <p className="mc-info">有未保存的绑定设置，请先保存或还原绑定，再编辑模型连接。</p>}
    <form onSubmit={save} className="mc-stack"><fieldset disabled={!editable || busy || bindingDirty}>
      <div className="mc-grid"><label>配置名称<input value={form.profile.display_name} required maxLength={128} onChange={e => profile({ display_name: e.target.value })} /></label>
        <label>接入方式<select value={form.profile.adapter_type} onChange={e => adapter(e.target.value as ProfileInput['adapter_type'])}>
          <option value="openai_compatible">OpenAI 兼容</option><option value="ollama_native">Ollama 原生</option>{role.model_kind === 'embedding' && <option value="dashscope_native">DashScope 原生向量</option>}</select></label>
        {role.model_kind === 'chat' && form.profile.adapter_type === 'openai_compatible' &&
          <label>聊天协议<select value={form.profile.api_protocol} onChange={e => profile({ api_protocol: e.target.value as ProfileInput['api_protocol'] })}>
            <option value="chat_completions">Chat Completions</option><option value="responses">Responses</option>
          </select></label>}
        <label>端点策略<select value={form.profile.endpoint_policy_revision_id} required onChange={e => { const next = policies.find(p => p.id === e.target.value); profile({ endpoint_policy_revision_id: e.target.value, ...(next ? { base_url: next.origin + next.allowed_path_prefix } : {}) }); }}>
          <option value="">请选择已授权端点</option>{activePolicies.map(p => <option key={p.id} value={p.id}>{p.policy_key} · v{p.revision} · {p.origin}</option>)}</select></label>
        <label>Base URL<input type="url" value={form.profile.base_url} required onChange={e => profile({ base_url: e.target.value })} /><span>兼容接口一般以 /v1 结尾；Ollama 原生使用服务根地址。</span></label>
        <label>模型名称<input value={form.profile.model} required maxLength={128} placeholder="例如 gpt-4.1 或 qwen3:8b" onChange={e => profile({ model: e.target.value })} /></label>
        <label>认证方式<select value={form.profile.auth_mode} onChange={e => { const mode = e.target.value as ProfileInput['auth_mode']; setForm(value => ({ ...value, profile: { ...value.profile, auth_mode: mode },
          keyAction: mode === 'none' ? credential?.has_api_key ? 'clear' : 'keep' : credential?.has_api_key ? 'keep' : 'replace', apiKey: '' })); }}>
          {form.profile.adapter_type !== 'dashscope_native' && <option value="none">无认证（本地 Ollama）</option>}{form.profile.adapter_type === 'dashscope_native' ? <option value="dashscope_key">DashScope Key</option> : <option value="bearer">Bearer API Key</option>}</select></label></div>
      <div className="mc-secret"><LockKeyhole size={17} aria-hidden="true" /><div><p>API Key：{credential?.has_api_key ? credential.masked_suffix ? `已保存 · ••••${credential.masked_suffix}` : '已保存' : '未配置'}{credential?.status === 'revoked' ? '（已撤销）' : ''}</p>
        <div className="mc-grid"><label>密钥操作<select value={form.keyAction} onChange={e => setForm({ ...form, keyAction: e.target.value as CredentialAction['action'], apiKey: '' })}>
          <option value="keep">保留已保存密钥</option><option value="replace" disabled={form.profile.auth_mode === 'none'}>替换为新密钥</option><option value="clear">清空密钥</option></select></label>
          {form.keyAction === 'replace' && <label>新 API Key<input type="password" autoComplete="off" value={form.apiKey} required spellCheck={false} onChange={e => setForm({ ...form, apiKey: e.target.value })} /></label>}</div>
        <p className="mc-muted">密钥只发送给后端。替换或清空前需要验证账号密码；修改服务地址后请重新配置密钥。</p></div></div>
      {role.model_kind !== 'embedding' ? <><label>系统提示词<textarea rows={6} value={form.system} onChange={e => setForm({ ...form, system: e.target.value })} placeholder="此角色的业务职责与回答规则" /></label>
        <label>任务模板<textarea rows={4} value={form.template} onChange={e => setForm({ ...form, template: e.target.value })} placeholder="填写此角色使用的模板正文" /></label><p className="mc-muted">可用变量：{role.template_variables?.map(name => `{${name}}`).join('、') || '以角色契约为准'}。固定安全策略由服务器提供。</p></>
        : <p className="mc-info">向量模型没有系统提示词。更换向量空间可能需要重建索引，当前不会切换现有索引。</p>}
      <details><summary>高级参数</summary><div className="mc-grid">
        <label>供应商<select value={form.profile.vendor} onChange={e => profile({ vendor: e.target.value as ProfileInput['vendor'] })}><option value="custom">其他兼容服务</option><option value="openai">OpenAI</option><option value="ollama">Ollama</option><option value="dashscope">DashScope</option></select></label>
        <label>连接超时（毫秒）<input type="number" min={1} max={90000} value={form.profile.connect_timeout_ms} onChange={e => profile({ connect_timeout_ms: Number(e.target.value) })} /></label>
        <label>请求超时（毫秒）<input type="number" min={1} max={300000} value={form.profile.request_timeout_ms} onChange={e => profile({ request_timeout_ms: Number(e.target.value) })} /></label>
        <label>上下文窗口（Token）<input type="number" min={1} max={4294967295} value={form.profile.context_window ?? ''}
          onChange={e => profile({ context_window: e.target.value ? Number(e.target.value) : null })} />
          <span>{role.model_kind === 'chat' ? '填写模型支持的总窗口，包含输入和输出。' : '填写模型支持的单条输入窗口。'}留空沿用原行为。</span></label>
        <label>{role.model_kind === 'embedding' ? '向量维度' : '最大输出 Token'}<input type="number" min={1} value={(role.model_kind === 'embedding' ? form.profile.embedding_dimension : form.profile.max_output_tokens) ?? ''} onChange={e => profile(role.model_kind === 'embedding' ? { embedding_dimension: e.target.value ? Number(e.target.value) : null } : { max_output_tokens: e.target.value ? Number(e.target.value) : null })} /></label>
        <label>Token 计数方式<select value={form.profile.token_counting_policy.mode === 'exact' ? 'exact' : 'conservative'}
          onChange={e => profile(e.target.value === 'exact' ? { token_counting_policy: { mode: 'exact' }, tokenizer_id: form.profile.tokenizer_id || 'cl100k_base' }
            : { token_counting_policy: { mode: 'conservative' }, tokenizer_id: null, tokenizer_revision: null })}>
          <option value="conservative">保守估算</option><option value="exact">使用本地分词器</option></select></label>
        {form.profile.token_counting_policy.mode === 'exact' && <label>本地词表<select value={form.profile.tokenizer_id ?? ''}
          onChange={e => profile({ tokenizer_id: e.target.value || null, tokenizer_revision: null })}>
          {!form.profile.tokenizer_id && <option value="">请选择词表</option>}
          {form.profile.tokenizer_id && !['cl100k_base', 'o200k_base'].includes(form.profile.tokenizer_id) &&
            <option value={form.profile.tokenizer_id}>{form.profile.tokenizer_id}（沿用现有设置）</option>}
          <option value="cl100k_base">cl100k_base</option><option value="o200k_base">o200k_base</option></select>
          <span>请选择与供应商模型匹配的词表。服务器缺少词表时使用保守估算。</span></label>}
      </div><label>模型参数（JSON）<textarea rows={3} value={form.parameters} onChange={e => setForm({ ...form, parameters: e.target.value })} spellCheck={false} /><span>{form.profile.api_protocol === 'responses' ? '支持 temperature 和 top_p。' : '支持 temperature、top_p、frequency_penalty、presence_penalty 和 seed。'}</span></label></details>
      <div className="mc-actions"><span className="mc-muted">{dirty ? '有未保存修改' : '与已保存草稿一致'}</span><button type="submit" className="mc-primary" disabled={!dirty || !editable || busy}><Save size={16} aria-hidden="true" />{busy ? '正在处理…' : '保存独立模型与提示词'}</button></div>
    </fieldset></form>
    {error && <div role="alert" className="mc-error">{error}{conflict && <p>请先复制要保留的内容，再使用页面顶部的“重新读取”。</p>}</div>}
    {notice && <p role="status" className="mc-info">{notice}</p>}
    <section className="mc-testing"><div className="mc-actions"><div><h3>能力测试</h3><p className="mc-muted">固定测试消息，最多 3 次模型请求，每次最多 96 输出 Token。可能产生供应商费用。</p></div>
      <button type="button" disabled={dirty || bindingDirty || busy || !testable || !resolved?.profile_revision_id} onClick={() => void test()}><FlaskConical size={16} aria-hidden="true" />{busy ? '正在测试…' : backup ? '测试主模型' : '测试已保存配置'}</button>
      {backup && <button type="button" disabled={dirty || bindingDirty || busy || !testable || !!pendingId} onClick={() => void test(backup)}>测试备用模型</button>}</div>
      <label className="mc-checkbox"><input type="checkbox" checked={cold} onChange={e => setCold(e.target.checked)} disabled={busy} />本地冷启动预算（90 秒，仅可信私网；默认 30 秒）</label>
      {dirty && <p className="mc-muted">请先保存修改，再测试对应版本。</p>}
      {validation && <div role="status" className="mc-test-result"><p><CheckCircle2 size={16} aria-hidden="true" />{statusName[validation.status]} · 草稿 v{validation.draft_version}{backup && (validation.fallback_role_key ? ' · 备用模型' : ' · 主模型')}</p>
        {validation.result?.checks?.map(check => <span key={check.capability} className="mc-badge">{capabilityName[check.capability] ?? check.capability}：{check.status === 'supported' ? '通过' : '未验证'}</span>)}
        {validation.result?.reason && <p className="mc-muted">原因：{validation.result.reason}</p>}<p className="mc-muted">测试通过仅表示本次协议/能力探测成功，尚未发布到业务调用。</p></div>}
    </section>
    <BindingEditor key={`${draft.id}:${draft.draft_version}:${role.role_key}`} draft={draft} role={role} roles={roles} fallbackEnabled={fallbackEnabled} busy={busy || dirty || !editable} onSave={saveBinding} onDirty={setBindingDirty} />
  </section>;
}

export default function ModelConfig() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [roles, setRoles] = useState<RoleDefinition[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [policyCursor, setPolicyCursor] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<DraftItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [selected, setSelected] = useState<RoleKey>('chat');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const [dirty, setDirty] = useState(false);
  const [proofResolver, setProofResolver] = useState<((proof: string | null) => void) | null>(null);
  const [releases, setReleases] = useState<Release[]>([]);
  const [activation, setActivation] = useState<'publish' | Release | null>(null);
  const [catchUp, setCatchUp] = useState<IndexCatchUp | null>(null);
  const [catchUpError, setCatchUpError] = useState('');
  const [mergeState, setMergeState] = useState<{ head: number; conflicts: RebaseConflict[] } | null>(null);
  const [mergeChoices, setMergeChoices] = useState<Record<string, 'ours' | 'theirs'>>({});
  const resolverRef = useRef<((proof: string | null) => void) | null>(null);
  const readController = useRef<AbortController | null>(null);
  const replay = useReplayKey();
  const historicalRelease = activation && activation !== 'publish' ? activation : null;
  const canCatchUp = Boolean(historicalRelease && historicalRelease.id !== summary?.active_release_id &&
    historicalRelease.role_bindings_raw?.embedding?.enabled && historicalRelease.index_bindings?.rag && historicalRelease.index_bindings?.notes);
  const currentCatchUp = catchUp?.restore_release_id === historicalRelease?.id ? catchUp : null;
  const pendingCatchUpId = currentCatchUp && ['queued', 'building', 'catching_up'].includes(currentCatchUp.status)
    ? currentCatchUp.id : null;
  useEffect(() => {
    if (!pendingCatchUpId) return;
    const jobId = pendingCatchUpId;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await modelConfigApi.indexCatchUp(jobId, controller.signal);
        if (controller.signal.aborted) return;
        setCatchUp(next);
        setCatchUpError('');
        if (['queued', 'building', 'catching_up'].includes(next.status)) timer = setTimeout(poll, 2000);
      } catch (failure) {
        if (!controller.signal.aborted) setCatchUpError(configError(failure).message);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [pendingCatchUpId]);
  useEffect(() => {
    const controller = new AbortController(); readController.current = controller;
    Promise.all([modelConfigApi.summary(controller.signal), modelConfigApi.roles(controller.signal),
      modelConfigApi.policies(undefined, controller.signal), modelConfigApi.drafts(undefined, controller.signal)])
      .then(async ([head, registry, endpoints, items]) => {
        if (controller.signal.aborted) return;
        setSummary(head); setRoles(registry.items); setPolicies(endpoints.items); setPolicyCursor(endpoints.next_cursor ?? null);
        if (head.features.publish) modelConfigApi.releases(controller.signal).then(page => {
          if (!controller.signal.aborted) setReleases(page.items);
        }).catch(() => {});
        setDrafts(items.items); setCursor(items.next_cursor ?? null);
        const first = items.items.find(item => ['editing', 'validating', 'ready'].includes(item.status));
        if (first) { const result = await modelConfigApi.draft(first.id, controller.signal); if (!controller.signal.aborted) setDraft(result); }
      }).catch(value => { if (!controller.signal.aborted) setError(configError(value).message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); readController.current?.abort(); resolverRef.current?.(null); };
  }, []);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [dirty]);
  const dirtyChange = useCallback((value: boolean) => setDirty(value), []);
  function canLeave() { return !dirty || window.confirm('当前角色有未保存的修改，确定放弃这些修改吗？'); }
  function update(next: Draft) { setDraft(next); setDirty(false); setSaved(`已保存草稿 v${next.draft_version}，尚未发布。`);
    setDrafts(items => items.map(item => item.id === next.id ? { ...item, draft_version: next.draft_version, status: next.status } : item)); }
  async function read(id: string) {
    if (!canLeave()) return;
    readController.current?.abort(); const controller = new AbortController(); readController.current = controller;
    setBusy(true); setError(''); setSaved('');
    try { const next = await modelConfigApi.draft(id, controller.signal); if (!controller.signal.aborted) { setDraft(next); setDirty(false); } }
    catch (value) { if (!controller.signal.aborted) setError(configError(value).message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function create() {
    if (!summary || !canLeave()) return; setBusy(true); setError('');
    try { const next = await modelConfigApi.createDraft(summary, replay.key(summary)); setDraft(next); setDrafts(items => [next, ...items]);
      setDirty(false); setSaved('已创建独立草稿。'); replay.clear(); }
    catch (value) { setError(configError(value).message); } finally { setBusy(false); }
  }
  async function more(kind: 'drafts' | 'policies') {
    setBusy(true); setError('');
    try { if (kind === 'drafts') { const next = await modelConfigApi.drafts(cursor ?? undefined); setDrafts(items => [...items, ...next.items]); setCursor(next.next_cursor ?? null); }
      else { const next = await modelConfigApi.policies(policyCursor ?? undefined); setPolicies(items => [...items, ...next.items]); setPolicyCursor(next.next_cursor ?? null); } }
    catch (value) { setError(configError(value).message); } finally { setBusy(false); }
  }
  const askProof: AskProof = () => new Promise(resolve => { resolverRef.current = resolve; setProofResolver(() => resolve); });
  async function rebase(resolutions: RebaseResolution[] = []) {
    if (!draft || dirty) return;
    setBusy(true); setError(''); setSaved('');
    try {
      const head = await modelConfigApi.summary(); setSummary(head);
      if (!mergeState && head.head_version === draft.base_head_version) {
        setSaved('草稿已基于最新发布，无需合并。'); return;
      }
      if (mergeState && mergeState.head !== head.head_version) {
        setMergeState(null); setMergeChoices({});
        setError('活动发布再次变化，请重新合并并核对冲突。'); return;
      }
      const signature = { action: 'rebase', id: draft.id, version: draft.draft_version, head: head.head_version, resolutions };
      const result = await modelConfigApi.rebase(draft, head.head_version, resolutions, replay.key(signature));
      update(result); setMergeState(null); setMergeChoices({}); replay.clear();
      setSaved(`已合并最新发布，草稿 v${result.draft_version}。请重新测试启用的角色后再发布。`);
    } catch (failure) {
      const detail = (failure as { response?: { data?: { code?: number; data?: { head_version: number; comparisons?: RebaseConflict[] } } } })?.response?.data;
      if (detail?.code === 40925 && detail.data?.comparisons?.length) {
        setMergeState({ head: detail.data.head_version, conflicts: detail.data.comparisons }); setMergeChoices({});
      } else { setError(configError(failure).message); setMergeState(null); }
    } finally { setBusy(false); }
  }
  async function activate() {
    if (!activation || !summary || (activation === 'publish' && !draft)) return;
    const action = activation; setActivation(null); setBusy(true); setError('');
    try {
      const proof = await askProof(); if (!proof) return;
      let result;
      if (action === 'publish') {
        if (!draft) return;
        const signature = { action: 'publish', id: draft.id, version: draft.draft_version, head: summary.head_version };
        result = await modelConfigApi.publish(draft, summary, replay.key(signature), proof);
        setDraft({ ...draft, status: 'published' });
        setDrafts(items => items.map(item => item.id === draft.id ? { ...item, status: 'published' } : item));
      } else {
        const signature = { action: 'restore', id: action.id, head: summary.head_version };
        result = await modelConfigApi.restore(action, summary, replay.key(signature), proof);
      }
      setSummary({ ...summary, active_release_id: result.release_id, head_version: result.head_version,
        runtime_source: 'database', setup_required: false });
      setSaved(`已生效：发布 ${result.release_id.slice(0, 8)}。新请求使用新配置，在途请求保持原版本。`);
      replay.clear();
      modelConfigApi.releases().then(page => setReleases(page.items)).catch(() => {});
    } catch (failure) { setError(configError(failure).message); }
    finally { setBusy(false); }
  }
  async function prepareHistory() {
    if (!historicalRelease || !summary) return;
    const release = historicalRelease;
    setActivation(null);
    setBusy(true);
    setCatchUpError('');
    try {
      const proof = await askProof();
      if (!proof) return;
      const signature = { action: 'index-catch-up', release: release.id, head: summary.head_version };
      const job = await modelConfigApi.catchUpRelease(release, summary, replay.key(signature), proof);
      setCatchUp(job);
      replay.clear();
    } catch (failure) {
      setCatchUpError(configError(failure).message);
    } finally {
      setBusy(false);
      setActivation(release);
    }
  }
  const editable = summary?.config_role_key !== 'config_reader' && Boolean(summary);
  const activeRole = roles.find(role => role.role_key === selected);
  return <main className="model-config"><header className="mc-page-heading"><div><p className="mc-eyebrow">AI SETTINGS</p><h1><Cpu size={27} aria-hidden="true" />模型配置</h1>
    <p className="mc-muted">一个页面管理各项 AI 功能的模型、端点、密钥与提示词。</p></div><span className="mc-badge">{roles.length || 17} 个独立角色</span></header>
    {loading ? <p role="status" className="mc-empty">正在读取模型配置…</p> : <>
      {error && <p role="alert" className="mc-error">{error}{!summary && ' 本地首次使用时，需要先按项目实施记录初始化配置管理员。'}</p>}
      {summary && <><div className="mc-info"><strong>{summary.features.publish ? summary.active_release_id ? `当前活动发布 ${summary.active_release_id.slice(0, 8)}` : '尚无活动发布' : '当前为草稿配置阶段'}</strong><p>{summary.features.publish ? '保存和测试只修改草稿；发布后新请求使用新版本，在途请求保持原版本。' : '保存和测试不会切换现有 AI 调用。发布与请求级热更新接入完成后才会开放生效操作。'}</p></div>
        <div className="mc-toolbar"><label>当前草稿<select value={draft?.id ?? ''} onChange={e => void read(e.target.value)} disabled={busy}>
          <option value="" disabled>请选择或新建草稿</option>{drafts.map(item => <option key={item.id} value={item.id}>{item.id.slice(0, 8)} · v{item.draft_version} · {statusName[item.status] ?? item.status}</option>)}</select></label>
          <div className="mc-actions">{cursor && <button disabled={busy} onClick={() => void more('drafts')}>更多草稿</button>}
            <button disabled={!draft || busy} onClick={() => draft && void read(draft.id)}><RefreshCw size={16} aria-hidden="true" />重新读取</button>
            <button disabled={!editable || busy} onClick={() => void create()}><Plus size={16} aria-hidden="true" />新建草稿</button>
            <button disabled={!editable || !draft || !['editing', 'validating', 'ready'].includes(draft.status) || dirty || busy}
              onClick={() => void rebase()}>合并最新发布</button>
            <button disabled={!summary.features.publish || summary.config_role_key !== 'config_admin' || !draft || draft.status === 'published' || dirty || busy}
              onClick={() => setActivation('publish')}>发布并生效{!summary.features.publish ? '（待接入）' : ''}</button></div></div>
        {saved && <p role="status" className="mc-success">{saved}</p>}
        {summary.config_role_key === 'config_reader' && <p className="mc-muted">当前为只读权限。</p>}
        {summary.config_role_key === 'config_admin' && <PolicyEditor askProof={askProof} onCreated={policy => { setPolicies(items => [policy, ...items]); setSaved('端点策略已保存，请在角色配置中选择。'); }} />}
        <ModelConfigCredentials canRevoke={summary.config_role_key === 'config_admin'} roles={roles} askProof={askProof} onRevoked={() => {
          setSaved('凭据版本已撤销。未保存的编辑内容保留，请配置新 Key 后重新测试和发布。');
          if (draft) modelConfigApi.draft(draft.id).then(next => setDraft(current => current?.id === next.id && current.draft_version === next.draft_version ? next : current)).catch(() => {});
        }} />
        {policyCursor && <button onClick={() => void more('policies')} disabled={busy}>加载更多端点策略</button>}
          {summary.config_role_key === 'config_admin' && <ModelConfigOutbox askProof={askProof} />}
        {releases.length > 0 && <details className="mc-policy"><summary>最近 20 次发布与恢复</summary><p className="mc-muted">恢复会创建新的发布，不修改旧记录；提交前仍检查权限、凭据、端点和兼容性。</p>
          {releases.map(release => <div className="mc-actions" key={release.id}><span>v{release.release_number} · {release.id.slice(0, 8)}{release.id === summary.active_release_id ? ' · 当前活动' : ''}</span>
            <button disabled={busy || dirty || summary.config_role_key !== 'config_admin'} onClick={() => { setCatchUpError(''); setActivation(release); }}>恢复为新发布</button></div>)}</details>}
        {draft && activeRole ? <div className="mc-workspace"><label className="mc-mobile-role">选择 AI 角色<select value={selected} onChange={e => { if (canLeave()) { setSelected(e.target.value as RoleKey); setDirty(false); setSaved(''); } }}>
          {roles.map(role => <option key={role.role_key} value={role.role_key}>{role.display_name}</option>)}</select></label><nav className="mc-roles" aria-label="AI 功能角色">
          {(['basic', 'advanced'] as const).map(group => <div key={group}><h2>{group === 'basic' ? '基本模型' : '高级业务模型'}</h2>{roles.filter(role => role.group === group).map(role => <button key={role.role_key} aria-current={selected === role.role_key ? 'true' : undefined} disabled={busy}
            onClick={() => { if (role.role_key === selected || !canLeave()) return; setSelected(role.role_key); setDirty(false); setSaved(''); }}><span>{role.display_name}</span><ChevronRight size={14} aria-hidden="true" /></button>)}</div>)}
        </nav><RoleEditor key={`${draft.id}:${draft.draft_version}:${selected}`} draft={draft} role={activeRole} roles={roles} policies={policies}
          editable={editable && ['editing', 'validating', 'ready'].includes(draft.status)} testable={editable && summary.features.capability_probe} fallbackEnabled={!!summary.features.fallback_model}
          askProof={askProof} update={update} dirtyChange={dirtyChange} /></div> : <div className="mc-empty"><Cpu size={32} aria-hidden="true" /><h2>从一份草稿开始</h2><p>新建草稿后，可逐项配置 AI 角色。</p></div>}
        {draft && selected === 'embedding' && <ModelConfigEmbedding key={`${draft.id}:${draft.draft_version}`} draftId={draft.id} />}
      </>}
    </>}
    <ReauthDialog key={proofResolver ? 'open' : 'closed'} resolver={proofResolver} close={() => { setProofResolver(null); resolverRef.current = null; }} />
    <Dialog.Root open={Boolean(mergeState)} onOpenChange={open => { if (!open && !busy) setMergeState(null); }}><Dialog.Portal>
      <Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog mc-merge-dialog"><Dialog.Title>选择冲突版本</Dialog.Title>
        <Dialog.Description>同一角色的模型、密钥、提示词与绑定作为整体保留。尚未修改草稿；请逐项选择，合并后需要重新测试。</Dialog.Description>
        {mergeState?.conflicts.map(item => <fieldset key={item.path} disabled={busy}><legend>{roles.find(role => role.role_key === item.path.split('/')[2])?.display_name ?? item.path}</legend>
          <details><summary>查看基准版本</summary><RebaseSummary value={item.base} /></details>
          {(['ours', 'theirs'] as const).map(choice => <label key={choice} className="mc-merge-choice"><span><input type="radio" name={item.path}
            checked={mergeChoices[item.path] === choice} onChange={() => setMergeChoices(values => ({ ...values, [item.path]: choice }))} />
            {choice === 'ours' ? '保留我的草稿' : '采用活动发布'}</span><RebaseSummary value={item[choice]} /></label>)}
        </fieldset>)}
        <div className="mc-actions"><button disabled={busy} onClick={() => setMergeState(null)}>取消</button>
          <button className="mc-primary" disabled={busy || !mergeState || mergeState.conflicts.some(item => !mergeChoices[item.path])}
            onClick={() => void rebase(mergeState!.conflicts.map(item => ({ path: item.path, choice: mergeChoices[item.path] })))}>{busy ? '正在合并…' : '按选择合并'}</button></div>
      </Dialog.Content></Dialog.Portal></Dialog.Root>
    <Dialog.Root open={Boolean(activation)} onOpenChange={open => { if (!open) setActivation(null); }}><Dialog.Portal>
      <Dialog.Overlay className="mc-overlay" /><Dialog.Content className="mc-dialog"><Dialog.Title>{activation === 'publish' ? '发布这份草稿' : '恢复为新的发布'}</Dialog.Title>
        <Dialog.Description>新请求会使用新配置，在途请求保持原版本。未启用的角色不会偷偷回退到环境变量模型。</Dialog.Description>
        {activation === 'publish' && draft && <><p>启用角色：{roles.filter(role => draft.payload.role_bindings_raw[role.role_key]?.enabled).map(role => role.display_name).join('、') || '无'}。</p>
          <p>未启用角色：{roles.filter(role => !draft.payload.role_bindings_raw[role.role_key]?.enabled).map(role => role.display_name).join('、')}。</p></>}
        <p>向量检索与向量写入仅使用本次发布绑定且已核验的集合与查询模型。缺少原始构建证据时，请保持向量角色停用；停用后相关调用会明确返回未配置错误。</p>
        <p>{summary?.features.fallback_model ? '发布前需要通过当前草稿的主模型和已配置备用模型测试。' : '当前后端版本暂不支持发布备用模型。'}</p>
        {canCatchUp && <>
          <p>追平会使用该版本的 Embedding 配置同步历史集合。</p>
          {currentCatchUp && <p role="status">{catchUpStatus[currentCatchUp.status] ?? currentCatchUp.status}
            {currentCatchUp.error_code && ` ${currentCatchUp.error_code}`}</p>}
          {catchUpError && <p role="alert" className="mc-error">{catchUpError}</p>}
        </>}
        <div className="mc-actions"><button onClick={() => setActivation(null)}>取消</button>
          {canCatchUp && <button className="mc-index-catch-up" disabled={Boolean(pendingCatchUpId && !catchUpError)}
            onClick={() => void prepareHistory()}>追平历史索引</button>}
          <button className="mc-primary" disabled={Boolean(pendingCatchUpId && !catchUpError)} onClick={() => void activate()}>确认并验证身份</button></div>
      </Dialog.Content></Dialog.Portal></Dialog.Root>
  </main>;
}
