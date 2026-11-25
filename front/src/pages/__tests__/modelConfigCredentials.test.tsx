// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { modelConfigApi, type CredentialImpact, type CredentialRevocation, type CredentialVersion, type RoleDefinition } from '../../api/modelConfig';
import ModelConfigCredentials from '../ModelConfigCredentials';

vi.mock('../../api/modelConfig', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/modelConfig')>();
  return { ...actual, modelConfigApi: { credentials: vi.fn(), credentialImpact: vi.fn(), revokeCredential: vi.fn() } };
});
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement; let root: Root;
const proof = vi.fn<() => Promise<string | null>>(); const changed = vi.fn();
const roles: RoleDefinition[] = [{ role_key: 'chat', display_name: '主对话', group: 'basic', model_kind: 'chat', prompt_key: 'main' }];
const credential: CredentialVersion = { id: 'key-one', credential_key: 'chat', credential_version: 2,
  allowed_origin: 'https://models.example.com', masked_suffix: '1234', status: 'active', revocation_epoch: 0, revoked_at: null };
const impact: CredentialImpact = { credential, active_release_id: 'release', active_role_keys: ['chat'],
  possible_dynamic_role_keys: ['synthesize'], active_index_keys: [], historical_release_count: 3,
  referenced_draft_count: 4, provider_key_revocation_supported: false };
const result: CredentialRevocation = { credential_revision_id: credential.id, credential_version: 2,
  status: 'revoked', revocation_epoch: 1, dispatch_blocked: true, cancel_requested: true, already_revoked: false };
beforeEach(() => {
  vi.resetAllMocks(); window.localStorage.clear(); window.sessionStorage.clear();
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
  vi.mocked(modelConfigApi.credentials).mockResolvedValue({ items: [credential] });
  vi.mocked(modelConfigApi.credentialImpact).mockResolvedValue(impact);
  vi.mocked(modelConfigApi.revokeCredential).mockResolvedValue(result); proof.mockResolvedValue('short-lived-proof');
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
async function mount(canRevoke = true) {
  await act(async () => root.render(<ModelConfigCredentials canRevoke={canRevoke} roles={roles} askProof={proof} onRevoked={changed} />));
  container.querySelector('details')!.open = true;
}
function button(text: string) { return [...document.querySelectorAll('button')].find(node => node.textContent === text)!; }
async function load() { await act(async () => button('读取凭据版本').click()); }
async function inspect() { await act(async () => button('查看影响并撤销').click()); }
async function confirm() {
  await act(async () => (document.querySelector('[role=dialog] input[type=checkbox]') as HTMLInputElement).click());
  await act(async () => button('确认范围并验证身份').click());
}

it('loads paginated masked versions only when requested', async () => {
  vi.mocked(modelConfigApi.credentials).mockResolvedValueOnce({ items: [credential], next_cursor: 'cursor' })
    .mockResolvedValueOnce({ items: [{ ...credential, id: 'key-two', credential_version: 3 }] });
  await mount(); expect(modelConfigApi.credentials).not.toHaveBeenCalled(); await load();
  expect(container.textContent).toContain('••••1234');
  await act(async () => button('更多凭据版本').click());
  expect(modelConfigApi.credentials).toHaveBeenLastCalledWith('cursor', expect.any(AbortSignal));
  expect(container.textContent).toContain('凭据 v3');
});

it('requires explicit scope confirmation and reauthentication before revoking', async () => {
  await mount(); await load(); await inspect();
  expect(modelConfigApi.revokeCredential).not.toHaveBeenCalled();
  expect(document.querySelector('[role=dialog]')?.textContent).toContain('历史发布：3');
  expect(button('确认范围并验证身份').disabled).toBe(true);
  await confirm();
  expect(proof).toHaveBeenCalledTimes(1);
  expect(modelConfigApi.revokeCredential).toHaveBeenCalledWith('key-one', 'suspected_leak', expect.any(String), 'short-lived-proof');
  expect(changed).toHaveBeenCalledWith('key-one');
  expect(container.textContent).toContain('正在进行的连接已请求停止');
  expect(container.textContent).toContain('已撤销');
  expect(window.localStorage.length).toBe(0); expect(window.sessionStorage.length).toBe(0);
});

it('does not revoke if reauthentication is dismissed', async () => {
  proof.mockResolvedValue(null); await mount(); await load(); await inspect(); await confirm();
  expect(modelConfigApi.revokeCredential).not.toHaveBeenCalled(); expect(changed).not.toHaveBeenCalled();
  expect(container.textContent).toContain('已发布');
});

it('keeps the same idempotency key for a network retry with fresh proof', async () => {
  vi.mocked(modelConfigApi.revokeCredential).mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(result);
  proof.mockResolvedValueOnce('proof-one').mockResolvedValueOnce('proof-two');
  await mount(); await load(); await inspect(); await confirm();
  expect(container.querySelector('[role=alert]')).not.toBeNull();
  const first = vi.mocked(modelConfigApi.revokeCredential).mock.calls[0];
  await inspect(); await confirm();
  const second = vi.mocked(modelConfigApi.revokeCredential).mock.calls[1];
  expect(second[2]).toBe(first[2]); expect(second[3]).toBe('proof-two');
  expect(changed).toHaveBeenCalledTimes(1);
});

it('allows read-only users to inspect but not revoke', async () => {
  await mount(false); await load(); await act(async () => button('查看影响').click());
  expect(document.querySelector('[role=dialog] input[type=checkbox]')).toBeNull();
  expect([...document.querySelectorAll('button')].some(node => node.textContent === '确认范围并验证身份')).toBe(false);
  expect(modelConfigApi.revokeCredential).not.toHaveBeenCalled();
});

it('shows an already-revoked version without an undo action', async () => {
  vi.mocked(modelConfigApi.credentials).mockResolvedValue({ items: [{ ...credential, status: 'revoked' }] });
  vi.mocked(modelConfigApi.credentialImpact).mockResolvedValue({ ...impact, credential: { ...credential, status: 'revoked' } });
  await mount(); await load(); await act(async () => button('查看影响').click());
  expect(document.querySelector('[role=dialog]')?.textContent).toContain('需要恢复调用时，请保存新 Key');
  expect(document.querySelector('[role=dialog] input')).toBeNull();
});

it('aborts a pending impact read and ignores its late result after unmount', async () => {
  let resolve!: (value: CredentialImpact) => void;
  vi.mocked(modelConfigApi.credentialImpact).mockReturnValue(new Promise(done => { resolve = done; }));
  await mount(); await load(); await act(async () => button('查看影响并撤销').click());
  const signal = vi.mocked(modelConfigApi.credentialImpact).mock.calls[0][1]!;
  await act(async () => root.render(null)); expect(signal.aborted).toBe(true);
  await act(async () => resolve(impact)); expect(document.querySelector('[role=dialog]')).toBeNull();
});
