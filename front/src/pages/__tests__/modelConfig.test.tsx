// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { modelConfigApi, type Draft, type ProfileInput, type RoleDefinition, type Summary } from '../../api/modelConfig';
import ModelConfig from '../ModelConfig';

vi.mock('../../api/modelConfig', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/modelConfig')>();
  return { ...actual, modelConfigApi: { summary: vi.fn(), roles: vi.fn(), policies: vi.fn(), drafts: vi.fn(),
    draft: vi.fn(), patch: vi.fn(), createDraft: vi.fn(), reauth: vi.fn(), test: vi.fn(), validation: vi.fn(),
    releases: vi.fn(), publish: vi.fn(), restore: vi.fn(), rebase: vi.fn(), embeddingStatus: vi.fn(),
    catchUpRelease: vi.fn(), indexCatchUp: vi.fn(),
    credentials: vi.fn(), credentialImpact: vi.fn(), revokeCredential: vi.fn(),
    outbox: vi.fn(), outboxEvent: vi.fn(), redriveOutbox: vi.fn() } };
});
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement;
let root: Root;
const keys = ['chat','chat_thinking','classifier','planner','vision','autocomplete','ppt_outline','embedding',
  'session_title','auto_tag','write_assistant','review_question','memory_summary','rag_summarize','synthesize','reflection','memory_extraction'] as const;
const roles: RoleDefinition[] = keys.map((role_key, index) => ({ role_key, display_name: index === 0 ? '主对话' : role_key,
  group: index < 8 ? 'basic' : 'advanced', model_kind: role_key === 'embedding' ? 'embedding' : 'chat', prompt_key: role_key === 'embedding' ? null : role_key }));
const profile: ProfileInput = { display_name: 'primary', model_kind: 'chat', adapter_type: 'openai_compatible',
  api_protocol: 'chat_completions', vendor: 'custom', base_url: 'https://models.example.com/v1', model: 'existing-model',
  model_revision: null, auth_mode: 'bearer', endpoint_policy_revision_id: 'policy', context_window: null,
  tokenizer_id: null, tokenizer_revision: null, token_counting_policy: {}, max_output_tokens: 200,
  connect_timeout_ms: 1000, request_timeout_ms: 3000, embedding_dimension: null, parameters: {} };
const summary: Summary = { config_role_key: 'config_admin', head_version: 0, active_release_id: null, setup_required: true,
  runtime_source: 'legacy_environment', features: { capability_probe: true, publish: false, hot_reload: false, personal_memory: false } };
function draft(): Draft {
  const bindings = Object.fromEntries(keys.map(key => [key, { enabled: false, selection: 'explicit' as const,
    profile_revision_id: key === 'chat' ? 'profile' : null, credential_revision_id: key === 'chat' ? 'credential' : null,
    prompt_revision_id: key === 'chat' ? 'prompt' : null, source_role: null, fallback: [], failure_mode: 'disabled' as const }]));
  return { id: 'draft', etag: '"draft:draft:2"', draft_version: 2, status: 'editing', base_head_version: 0,
    created_at: '2026-11-16T00:00:00Z', payload: { schema_version: 2, role_bindings_raw: bindings as unknown as Draft['payload']['role_bindings_raw'], index_bindings: {} },
    profiles: [{ ...profile, id: 'profile', revision: 1, credential_revision_id: 'credential' }],
    prompts: [{ id: 'prompt', system_text: 'existing system', template_text: 'template' }],
    resolved_static_bindings: bindings as Draft['resolved_static_bindings'],
    credential_states: [{ role_key: 'chat', has_api_key: true, masked_suffix: '1234', status: 'candidate' }],
    validation_coverage: [], binding_issues: [], publication_blockers: ['RUNTIME_INTEGRATION_PENDING'] };
}
beforeEach(() => {
  vi.resetAllMocks();
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
  vi.mocked(modelConfigApi.summary).mockResolvedValue(summary);
  vi.mocked(modelConfigApi.roles).mockResolvedValue({ items: roles });
  vi.mocked(modelConfigApi.policies).mockResolvedValue({ items: [{ id: 'policy', revision: 1, status: 'active',
    policy_key: 'fixture', origin: 'https://models.example.com', allowed_path_prefix: '/v1', address_mode: 'public_https',
    environment: 'development', connection_rules: { allowed_cidrs: [] } }] });
  vi.mocked(modelConfigApi.drafts).mockResolvedValue({ items: [{ id: 'draft', draft_version: 2, status: 'editing' }] });
  vi.mocked(modelConfigApi.draft).mockResolvedValue(draft());
  vi.mocked(modelConfigApi.embeddingStatus).mockResolvedValue({ items: [], draft: null, head_version: 0,
    registration_status: 'not_registered', migration_supported: false, runtime_binding_supported: false });
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); });
async function mount() { await act(async () => root.render(<ModelConfig />)); }
function button(text: string) { return [...container.querySelectorAll('button')].find(node => node.textContent?.includes(text))!; }
function field(text: string) {
  const label = [...container.querySelectorAll('label')].find(node => node.textContent?.startsWith(text))!;
  return label.querySelector('input,textarea,select') as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
}
async function input(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
}
async function save() { await act(async () => button('保存独立模型与提示词').closest('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))); }

it('configures the optional expansion role independently on an existing v2 draft', async () => {
  vi.mocked(modelConfigApi.roles).mockResolvedValue({ items: [...roles, { role_key: 'rag_query_expand',
    display_name: '检索查询扩展', group: 'advanced', model_kind: 'chat', prompt_key: 'rag_query_expand',
    template_variables: ['query'], default_template: '当前问题：{query}' }] });
  vi.mocked(modelConfigApi.patch).mockResolvedValue(draft());
  await mount();
  await act(async () => button('检索查询扩展').click());
  expect(field('任务模板').value).toBe('当前问题：{query}');
  expect(field('模型名称').value).toBe('');
  expect((field('模型选择方式') as HTMLSelectElement).options.length).toBe(1);
  await input(field('接入方式'), 'ollama_native');
  await input(field('Base URL'), 'http://localhost:11434');
  await input(field('模型名称'), 'query-model');
  await input(field('系统提示词'), '保留型号');
  await save();
  expect(modelConfigApi.patch).toHaveBeenCalledWith(draft(), [
    expect.objectContaining({ role_key: 'rag_query_expand', op: 'replace_profile', profile: expect.objectContaining({
      api_protocol: 'ollama_chat', base_url: 'http://localhost:11434', model: 'query-model', auth_mode: 'none' }) }),
    { role_key: 'rag_query_expand', op: 'replace_prompt', prompt: { system_text: '保留型号', template_text: '当前问题：{query}' } }
  ], expect.any(String), undefined);
});

it('saves Responses only when the operator selects that protocol', async () => {
  vi.mocked(modelConfigApi.patch).mockResolvedValue(draft());
  await mount();
  const protocol = field('聊天协议');
  expect(protocol.value).toBe('chat_completions');
  await input(protocol, 'responses');
  await save();
  expect(modelConfigApi.patch).toHaveBeenCalledWith(draft(), expect.arrayContaining([
    expect.objectContaining({ op: 'replace_profile', profile: expect.objectContaining({ api_protocol: 'responses' }) }),
  ]), expect.any(String), undefined);
});

it('saves context and tokenizer settings independently of backup model support', async () => {
  vi.mocked(modelConfigApi.patch).mockResolvedValue(draft());
  await mount();
  await input(field('上下文窗口'), '8192');
  await input(field('Token 计数方式'), 'exact');
  await input(field('本地词表'), 'o200k_base');
  await save();
  expect(modelConfigApi.patch).toHaveBeenCalledWith(draft(), expect.arrayContaining([
    expect.objectContaining({ op: 'replace_profile', role_key: 'chat', profile: expect.objectContaining({
      context_window: 8192, tokenizer_id: 'o200k_base', token_counting_policy: { mode: 'exact' },
    }) }),
  ]), expect.any(String), undefined);
});

it('saves a backup binding and tests it with the source role', async () => {
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, features: { ...summary.features, fallback_model: true } });
  const initial = draft();
  initial.payload.role_bindings_raw.chat.enabled = true;
  initial.payload.role_bindings_raw.session_title.enabled = true;
  initial.payload.role_bindings_raw.session_title.profile_revision_id = 'backup';
  const saved = structuredClone(initial);
  saved.payload.role_bindings_raw.chat.fallback = ['session_title'];
  saved.draft_version = 3;
  saved.etag = '"draft:draft:3"';
  vi.mocked(modelConfigApi.draft).mockResolvedValue(initial);
  vi.mocked(modelConfigApi.patch).mockResolvedValue(saved);
  vi.mocked(modelConfigApi.test).mockResolvedValue({ id: 'backup-test', request_record_id: 'request', role_key: 'chat',
    fallback_role_key: 'session_title', draft_version: 3, status: 'passed', result: null });
  await mount();
  await input(field('备用模型角色'), 'session_title');
  await act(async () => button('保存绑定设置').click());
  expect(modelConfigApi.patch).toHaveBeenCalledWith(initial, [{ op: 'replace_binding', role_key: 'chat',
    binding: expect.objectContaining({ fallback: ['session_title'] }) }], expect.any(String));
  await act(async () => button('测试备用模型').click());
  expect(modelConfigApi.test).toHaveBeenCalledWith(saved, 'chat', expect.any(String), false, 'session_title');
  expect(container.textContent).toContain('测试通过 · 草稿 v3 · 备用模型');
});

it('keeps unsaved role text after credential revocation and refreshes metadata only', async () => {
  await mount(); await input(field('系统提示词'), 'keep unsaved system prompt');
  const key = { id: 'credential', credential_key: 'chat', credential_version: 1, allowed_origin: 'https://models.example.com',
    masked_suffix: '1234', status: 'candidate' as const, revocation_epoch: 0, revoked_at: null };
  vi.mocked(modelConfigApi.credentials).mockResolvedValue({ items: [key] });
  vi.mocked(modelConfigApi.credentialImpact).mockResolvedValue({ credential: key, active_release_id: null, active_role_keys: [],
    possible_dynamic_role_keys: [], active_index_keys: [], historical_release_count: 0, referenced_draft_count: 1, provider_key_revocation_supported: false });
  vi.mocked(modelConfigApi.reauth).mockResolvedValue({ proof: 'proof' });
  vi.mocked(modelConfigApi.revokeCredential).mockResolvedValue({ credential_revision_id: 'credential', credential_version: 1,
    status: 'revoked', revocation_epoch: 1, dispatch_blocked: true, cancel_requested: true, already_revoked: false });
  vi.mocked(modelConfigApi.draft).mockResolvedValue({ ...draft(), credential_states: [{ role_key: 'chat', has_api_key: true, masked_suffix: '1234', status: 'revoked' }] });
  await act(async () => button('读取凭据版本').click());
  await act(async () => button('查看影响并撤销').click());
  await act(async () => (document.querySelector('[role=dialog] input[type=checkbox]') as HTMLInputElement).click());
  const confirm = [...document.querySelectorAll('[role=dialog] button')].find(node => node.textContent === '确认范围并验证身份') as HTMLButtonElement;
  await act(async () => confirm.click());
  const password = document.querySelector('[role=dialog] input') as HTMLInputElement;
  await input(password, 'account-password');
  await act(async () => password.closest('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(field('系统提示词').value).toBe('keep unsaved system prompt');
  expect(container.textContent).toContain('未保存的编辑内容保留');
  expect(button('发布并生效').disabled).toBe(true);
});

it('loads vector baseline status when opening the embedding role without changing the draft', async () => {
  await mount();
  expect(modelConfigApi.embeddingStatus).not.toHaveBeenCalled();
  await act(async () => button('embedding').click());
  expect(modelConfigApi.embeddingStatus).toHaveBeenCalledWith('draft', expect.any(AbortSignal));
  expect(container.textContent).toContain('尚未登记现有集合');
  expect(modelConfigApi.patch).not.toHaveBeenCalled();
});

it('merges against a refreshed head and requires fresh validation', async () => {
  await mount();
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, head_version: 1 });
  vi.mocked(modelConfigApi.rebase).mockResolvedValue({ ...draft(), draft_version: 3, base_head_version: 1, validation_coverage: [] });
  await act(async () => button('合并最新发布').click());
  expect(modelConfigApi.rebase).toHaveBeenCalledWith(expect.objectContaining({ etag: '"draft:draft:2"' }), 1, [], expect.any(String));
  expect(container.textContent).toContain('请重新测试启用的角色');
});

it('requires explicit conflict choices and preserves one atomic role', async () => {
  await mount();
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, head_version: 1 });
  vi.mocked(modelConfigApi.rebase).mockRejectedValueOnce({ response: { data: { code: 40925, data: { head_version: 1,
    comparisons: [{ path: '/role_bindings_raw/chat', base: null, ours: { model: 'local' }, theirs: { model: 'remote' } }] } } } });
  await act(async () => button('合并最新发布').click());
  const dialog = document.querySelector('[role=dialog]')!;
  const confirm = [...dialog.querySelectorAll('button')].find(b => b.textContent === '按选择合并')!;
  expect(confirm.disabled).toBe(true);
  vi.mocked(modelConfigApi.rebase).mockResolvedValue({ ...draft(), draft_version: 3, base_head_version: 1 });
  await act(async () => (dialog.querySelectorAll('input[type=radio]')[1] as HTMLInputElement).click());
  await act(async () => confirm.click());
  expect(modelConfigApi.rebase).toHaveBeenLastCalledWith(expect.objectContaining({ draft_version: 2 }), 1,
    [{ path: '/role_bindings_raw/chat', choice: 'theirs' }], expect.any(String));
  expect(document.querySelector('[role=dialog]')).toBeNull();
});

it('blocks merging unsaved edits and avoids rewriting an already current draft', async () => {
  await mount(); await act(async () => button('合并最新发布').click());
  expect(modelConfigApi.rebase).not.toHaveBeenCalled();
  expect(container.textContent).toContain('无需合并');
  await input(field('系统提示词'), 'keep my unsaved text');
  expect(button('合并最新发布').disabled).toBe(true);
  expect(field('系统提示词').value).toBe('keep my unsaved text');
});

it('discards stale conflict choices when the active head changes again', async () => {
  await mount(); vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, head_version: 1 });
  vi.mocked(modelConfigApi.rebase).mockRejectedValue({ response: { data: { code: 40925, data: { head_version: 1,
    comparisons: [{ path: '/role_bindings_raw/chat', base: null, ours: null, theirs: null }] } } } });
  await act(async () => button('合并最新发布').click());
  await act(async () => (document.querySelector('[role=dialog] input') as HTMLInputElement).click());
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, head_version: 2 });
  const confirm = [...document.querySelectorAll('[role=dialog] button')].find(b => b.textContent === '按选择合并') as HTMLButtonElement;
  await act(async () => confirm.click());
  expect(modelConfigApi.rebase).toHaveBeenCalledTimes(1);
  expect(container.textContent).toContain('活动发布再次变化');
});

async function confirmActivation(label = '确认并验证身份') {
  const confirm = [...document.querySelectorAll('[role=dialog] button')].find(node => node.textContent?.includes(label)) as HTMLButtonElement;
  await act(async () => confirm.click());
  const password = document.querySelector('[role=dialog] input') as HTMLInputElement;
  await input(password, 'account-password');
  await act(async () => password.closest('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
}

it('confirms disabled roles and vector limits, then publishes with reauthentication and CAS', async () => {
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, features: { ...summary.features, publish: true } });
  vi.mocked(modelConfigApi.releases).mockResolvedValue({ items: [] });
  vi.mocked(modelConfigApi.reauth).mockResolvedValue({ proof: 'proof' });
  vi.mocked(modelConfigApi.publish).mockResolvedValue({ release_id: 'release-new', head_version: 1, release_number: 1 });
  await mount(); await act(async () => button('发布并生效').click());
  expect(modelConfigApi.publish).not.toHaveBeenCalled();
  expect(document.querySelector('[role=dialog]')?.textContent).toContain('向量检索与向量写入仅使用本次发布绑定且已核验');
  await confirmActivation();
  expect(modelConfigApi.publish).toHaveBeenCalledWith(expect.objectContaining({ etag: '"draft:draft:2"' }),
    expect.objectContaining({ head_version: 0 }), expect.any(String), 'proof');
  expect(container.textContent).toContain('已生效：发布 release-');
  expect(button('发布并生效').disabled).toBe(true);
});

it('restores a historic release through a new publication', async () => {
  const old = { id: 'historical', release_number: 1, created_at: '2026-11-16T00:00:00Z' };
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, head_version: 2, features: { ...summary.features, publish: true } });
  vi.mocked(modelConfigApi.releases).mockResolvedValue({ items: [old] });
  vi.mocked(modelConfigApi.reauth).mockResolvedValue({ proof: 'proof' });
  vi.mocked(modelConfigApi.restore).mockResolvedValue({ release_id: 'restored-new', head_version: 3 });
  await mount(); await act(async () => button('恢复为新发布').click()); await confirmActivation();
  expect(modelConfigApi.restore).toHaveBeenCalledWith(old, expect.objectContaining({ head_version: 2 }), expect.any(String), 'proof');
  expect(modelConfigApi.publish).not.toHaveBeenCalled(); expect(container.textContent).toContain('当前活动发布 restored');
});

it('prepares stale historical indexes and restores after the job is ready without an open draft', async () => {
  const old = { id: 'historical', release_number: 1, created_at: '2026-11-16T00:00:00Z',
    role_bindings_raw: { embedding: { enabled: true } }, index_bindings: { rag: {}, notes: {} } };
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, head_version: 2, active_release_id: 'current',
    features: { ...summary.features, publish: true } });
  vi.mocked(modelConfigApi.drafts).mockResolvedValue({ items: [] });
  vi.mocked(modelConfigApi.releases).mockResolvedValue({ items: [old] });
  vi.mocked(modelConfigApi.reauth).mockResolvedValue({ proof: 'proof' });
  const job = { id: 'job', restore_release_id: old.id, status: 'queued', error_code: null };
  vi.mocked(modelConfigApi.catchUpRelease).mockResolvedValue(job);
  vi.mocked(modelConfigApi.indexCatchUp).mockResolvedValue({ ...job, status: 'ready_to_switch' });
  vi.mocked(modelConfigApi.restore).mockResolvedValue({ release_id: 'restored-new', head_version: 3 });
  await mount();
  await act(async () => button('恢复为新发布').click());
  await confirmActivation('追平历史索引');
  expect(modelConfigApi.catchUpRelease).toHaveBeenCalledWith(old, expect.objectContaining({ head_version: 2 }), expect.any(String), 'proof');
  expect(modelConfigApi.indexCatchUp).toHaveBeenCalledWith('job', expect.any(AbortSignal));
  expect(document.querySelector('[role=dialog] [role=status]')?.textContent).toContain('历史索引已追平');
  expect(modelConfigApi.restore).not.toHaveBeenCalled();
  await confirmActivation();
  expect(modelConfigApi.restore).toHaveBeenCalledWith(old, expect.objectContaining({ head_version: 2 }), expect.any(String), 'proof');
});

it('shows all 17 roles in one page with an honest draft state and unavailable publish', async () => {
  await mount();
  expect(container.querySelectorAll('.mc-roles button')).toHaveLength(17);
  expect(container.textContent).toContain('保存和测试不会切换现有 AI 调用');
  expect(button('发布并生效').disabled).toBe(true);
  expect(container.querySelector('input[type=password]')).toBeNull();
  expect(container.textContent).toContain('••••1234');
});

it('saves model and prompt atomically without sending an unchanged secret', async () => {
  await mount();
  vi.mocked(modelConfigApi.patch).mockResolvedValue({ ...draft(), draft_version: 3, etag: '"draft:draft:3"' });
  await input(field('模型名称'), 'new-model'); await save();
  const [old, operations] = vi.mocked(modelConfigApi.patch).mock.calls[0];
  expect(old.etag).toBe('"draft:draft:2"');
  expect(operations[0]).toMatchObject({ op: 'replace_profile', role_key: 'chat', profile: { model: 'new-model' }, credential: { action: 'keep' } });
  expect(operations[1]).toMatchObject({ op: 'replace_prompt', role_key: 'chat' });
  expect(modelConfigApi.reauth).not.toHaveBeenCalled();
  expect(container.textContent).toContain('已保存草稿 v3，尚未发布');
});

it('retains unsaved text on conflict and uses the same idempotency key on retry', async () => {
  await mount();
  vi.mocked(modelConfigApi.patch).mockRejectedValue({ response: { status: 409, data: { code: 40921 } } });
  await input(field('系统提示词'), 'my retained prompt'); await save();
  expect((field('系统提示词') as HTMLTextAreaElement).value).toBe('my retained prompt');
  expect(container.textContent).toContain('你的未保存内容已保留');
  await save();
  expect(vi.mocked(modelConfigApi.patch).mock.calls[0][2]).toBe(vi.mocked(modelConfigApi.patch).mock.calls[1][2]);
  expect(modelConfigApi.draft).toHaveBeenCalledTimes(1);
});

it('requests reauthentication only when replacing a secret and clears plaintext after success', async () => {
  await mount();
  await input(field('密钥操作'), 'replace'); await input(field('新 API Key'), 'test-key-not-for-storage');
  vi.mocked(modelConfigApi.reauth).mockResolvedValue({ proof: 'temporary-proof' });
  vi.mocked(modelConfigApi.patch).mockResolvedValue({ ...draft(), draft_version: 3, etag: 'new' });
  await save();
  expect(document.querySelector('[role=dialog]')).not.toBeNull();
  const password = document.querySelector('[role=dialog] input') as HTMLInputElement;
  await input(password, 'account-password');
  await act(async () => password.closest('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(vi.mocked(modelConfigApi.patch).mock.calls[0][3]).toBe('temporary-proof');
  expect(vi.mocked(modelConfigApi.patch).mock.calls[0][1][0]).toMatchObject({ credential: { action: 'replace', api_key: 'test-key-not-for-storage' } });
  expect(document.querySelector('[role=dialog]')).toBeNull();
  expect([...container.querySelectorAll('input')].some(node => node.value === 'test-key-not-for-storage')).toBe(false);
  expect(JSON.stringify(localStorage)).not.toContain('test-key-not-for-storage');
});

it('keeps reader controls disabled and does not call write or test APIs', async () => {
  vi.mocked(modelConfigApi.summary).mockResolvedValue({ ...summary, config_role_key: 'config_reader' });
  await mount();
  expect(container.textContent).toContain('当前为只读权限');
  expect(button('新建草稿').disabled).toBe(true);
  expect(button('测试已保存配置').disabled).toBe(true);
  expect((field('模型名称').closest('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
  expect(modelConfigApi.patch).not.toHaveBeenCalled();
});

it('blocks tests for unsaved edits and preserves edits when role switching is cancelled', async () => {
  await mount();
  await input(field('模型名称'), 'edited-model');
  expect(button('测试已保存配置').disabled).toBe(true);
  vi.spyOn(window, 'confirm').mockReturnValue(false);
  await act(async () => button('chat_thinking').click());
  expect((field('模型名称') as HTMLInputElement).value).toBe('edited-model');
  expect(container.querySelector('[aria-current=true]')?.textContent).toContain('主对话');
});

it('displays unverified provider failure without claiming that configuration is active', async () => {
  await mount();
  vi.mocked(modelConfigApi.test).mockResolvedValue({ id: 'v', request_record_id: 'r', role_key: 'chat', draft_version: 2,
    status: 'unverified', result: { reason: 'PROVIDER_AUTH_FAILED' } });
  await act(async () => button('测试已保存配置').click());
  expect(container.textContent).toContain('尚未验证');
  expect(container.textContent).toContain('PROVIDER_AUTH_FAILED');
  expect(modelConfigApi.test).toHaveBeenCalledWith(expect.objectContaining({ draft_version: 2 }), 'chat', expect.any(String), false);
});
