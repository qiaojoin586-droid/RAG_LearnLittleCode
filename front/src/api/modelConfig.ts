import client from './client';
import type { ApiResponse } from '../types/api';

export type RoleKey = 'chat' | 'chat_thinking' | 'classifier' | 'planner' | 'vision' | 'autocomplete'
  | 'ppt_outline' | 'embedding' | 'session_title' | 'auto_tag' | 'write_assistant' | 'review_question'
  | 'memory_summary' | 'rag_summarize' | 'synthesize' | 'reflection' | 'memory_extraction' | 'rag_query_expand';
export interface RoleDefinition { role_key: RoleKey; display_name: string; group: 'basic' | 'advanced'; model_kind: 'chat' | 'embedding'; prompt_key: string | null; default_template?: string; template_variables?: string[] }
export interface Release {
  id: string; release_number: number; created_at: string;
  index_bindings?: Record<string, unknown>;
  role_bindings_raw?: { embedding?: { enabled: boolean } };
}
export interface IndexCatchUp {
  id: string; restore_release_id: string; status: string; error_code: string | null;
}
export interface CredentialVersion {
  id: string; credential_key: string; credential_version: number; allowed_origin: string; masked_suffix: string | null;
  status: 'candidate' | 'active' | 'retired' | 'revoked'; revocation_epoch: number; revoked_at: string | null;
}
export interface CredentialImpact {
  credential: CredentialVersion; active_release_id: string | null; active_role_keys: RoleKey[];
  possible_dynamic_role_keys: RoleKey[]; active_index_keys: string[];
  historical_release_count: number; referenced_draft_count: number;
  provider_key_revocation_supported: false;
}
export interface CredentialRevocation {
  credential_revision_id: string; credential_version: number; status: 'revoked'; revocation_epoch: number;
  dispatch_blocked: true; cancel_requested: true; already_revoked: boolean;
}
export interface RebaseResolution { path: string; choice: 'ours' | 'theirs' }
export interface RebaseConflict {
  path: string;
  base: Record<string, unknown> | null; ours: Record<string, unknown> | null; theirs: Record<string, unknown> | null;
}
export interface Binding {
  enabled: boolean; selection: 'explicit' | 'follow_role' | 'follow_execution';
  profile_revision_id?: string | null; credential_revision_id?: string | null; prompt_revision_id: string | null;
  source_role?: RoleKey | null; fallback: RoleKey[];
  failure_mode: 'error' | 'rule_only' | 'react_fallback' | 'disabled' | 'default_title';
}
export interface ProfileInput {
  display_name: string; model_kind: 'chat' | 'embedding'; adapter_type: 'openai_compatible' | 'ollama_native' | 'dashscope_native';
  api_protocol: 'chat_completions' | 'responses' | 'openai_embeddings' | 'ollama_chat' | 'ollama_embed' | 'dashscope_embed';
  vendor: 'custom' | 'openai' | 'ollama' | 'dashscope'; base_url: string; model: string; model_revision: string | null;
  auth_mode: 'none' | 'bearer' | 'dashscope_key'; endpoint_policy_revision_id: string;
  context_window: number | null; tokenizer_id: string | null; tokenizer_revision: string | null;
  token_counting_policy: Record<string, unknown>; max_output_tokens: number | null;
  connect_timeout_ms: number; request_timeout_ms: number; embedding_dimension: number | null;
  parameters: Record<string, unknown>;
}
export interface Profile extends ProfileInput { id: string; revision: number; credential_revision_id: string | null }
export interface Prompt { id: string; system_text: string; template_text: string }
export interface PolicyInput { policy_key: string; origin: string; allowed_path_prefix: string; address_mode: 'public_https' | 'trusted_private'; connection_rules: { allowed_cidrs: string[]; allow_cross_service_fallback?: boolean }; environment: string }
export interface Policy extends PolicyInput { id: string; revision: number; status: string }
export interface Validation {
  id: string; request_record_id: string; role_key: RoleKey; draft_version: number;
  status: 'running' | 'passed' | 'failed' | 'unverified' | 'pending' | 'cancelled';
  result: { reason?: string; call_count?: number; checks?: { capability: string; status: string }[]; dimension?: number } | null;
  valid_until?: string | null;
  fallback_role_key?: RoleKey | null;
}
export interface Draft {
  id: string; etag: string; draft_version: number; status: string; base_head_version: number; created_at: string;
  payload: { schema_version: 2 | 3; role_bindings_raw: Record<Exclude<RoleKey, 'rag_query_expand'>, Binding> & { rag_query_expand?: Binding }; index_bindings: Record<string, unknown> };
  profiles: Profile[]; prompts: Prompt[]; resolved_static_bindings: Partial<Record<RoleKey, Binding>>;
  credential_states: { role_key: RoleKey; has_api_key: boolean | null; masked_suffix: string | null; status: string }[];
  validation_coverage: Validation[]; binding_issues: { role_key: RoleKey; reason: string }[];
  publication_blockers: string[];
}
export interface Summary {
  config_role_key: 'config_reader' | 'config_operator' | 'config_admin'; head_version: number;
  active_release_id: string | null; setup_required: boolean; runtime_source: string;
  features: { capability_probe: boolean; publish: boolean; hot_reload: boolean; personal_memory: boolean; fallback_model?: boolean };
}
export interface EmbeddingStatus {
  registration_status: 'registered' | 'not_registered'; migration_supported: boolean; runtime_binding_supported: boolean;
  head_version: number;
  items: { id: string; index_key: string; index_version: number; collection_name: string; dimension: number;
    status: string; metadata_record_count: number | null; semantic_provenance_verified: boolean; runtime_usable: boolean; created_at: string }[];
  draft: { id: string; draft_version: number; embedding_enabled: boolean;
    embedding_diff: { index_key: string; status: string; reason: string }[]; publication_blockers: string[] } | null;
}
export type CredentialAction = { action: 'keep' | 'clear' } | { action: 'replace'; api_key: string };
export type Operation =
  | { op: 'replace_profile'; role_key: RoleKey; profile: ProfileInput; credential: CredentialAction; index_keys?: string[] }
  | { op: 'replace_prompt'; role_key: RoleKey; prompt: { system_text: string; template_text: string } }
  | { op: 'replace_binding'; role_key: RoleKey; binding: Binding };
export type OutboxStatus = 'pending' | 'inflight' | 'succeeded' | 'retry' | 'dead_letter';
export interface OutboxEvent {
  id: string; event_type: 'model_config.published' | 'credential.revoked'; aggregate_id: string; aggregate_version: number;
  status: OutboxStatus; attempts: number; next_attempt_at: string; lease_expires_at: string | null;
  error_code: string | null; created_at: string; redrive_event_id: string | null;
}
export interface OutboxRedrive {
  source_event_id: string; event_id: string; status: 'pending'; attempts: 0; original_attempts: number;
  delivery_guarantee: 'at_least_once'; config_head_changed: false;
}
interface Page<T> { items: T[]; next_cursor?: string | null }
const base = '/api/v1/model-config';
const headers = (key: string, etag?: string, proof?: string) => ({ 'Idempotency-Key': key,
  ...(etag ? { 'If-Match': etag } : {}), ...(proof ? { 'X-Admin-Reauth': proof } : {}) });
const data = <T>(res: { data: ApiResponse<T> }) => res.data.data;

export const modelConfigApi = {
  outbox: (status?: OutboxStatus, cursor?: string, signal?: AbortSignal) =>
    client.get<ApiResponse<Page<OutboxEvent>>>(`${base}/outbox`, { params: { status, cursor }, signal }).then(data),
  outboxEvent: (id: string, signal?: AbortSignal) => client.get<ApiResponse<OutboxEvent>>(`${base}/outbox/${id}`, { signal }).then(data),
  redriveOutbox: (id: string, attempts: number, key: string, proof: string) =>
    client.post<ApiResponse<OutboxRedrive>>(`${base}/outbox/${id}/redrive`,
      { expected_attempts: attempts, reason: 'operator_retry' }, { headers: headers(key, undefined, proof) }).then(data),
  summary: (signal?: AbortSignal) => client.get<ApiResponse<Summary>>(base, { signal }).then(data),
  credentials: (cursor?: string, signal?: AbortSignal) => client.get<ApiResponse<Page<CredentialVersion>>>(
    `${base}/credentials`, { params: { cursor }, signal }).then(data),
  credentialImpact: (id: string, signal?: AbortSignal) => client.get<ApiResponse<CredentialImpact>>(
    `${base}/credentials/${id}/impact`, { signal }).then(data),
  revokeCredential: (id: string, reason: string, key: string, proof: string) => client.post<ApiResponse<CredentialRevocation>>(
    `${base}/credentials/${id}/revoke`, { reason }, { headers: headers(key, undefined, proof) }).then(data),
  embeddingStatus: (draftId?: string, signal?: AbortSignal) => client.get<ApiResponse<EmbeddingStatus>>(
    `${base}/embedding-status`, { params: { draft_id: draftId }, signal }).then(data),
  roles: (signal?: AbortSignal) => client.get<ApiResponse<Page<RoleDefinition>>>(`${base}/roles`, { signal }).then(data),
  drafts: (cursor?: string, signal?: AbortSignal) => client.get<ApiResponse<Page<Pick<Draft, 'id' | 'draft_version' | 'status'>>>>(`${base}/drafts`, { params: { cursor }, signal }).then(data),
  draft: (id: string, signal?: AbortSignal) => client.get<ApiResponse<Draft>>(`${base}/drafts/${id}`, { signal }).then(data),
  policies: (cursor?: string, signal?: AbortSignal) => client.get<ApiResponse<Page<Policy>>>(`${base}/endpoint-policies`, { params: { cursor }, signal }).then(data),
  createDraft: (summary: Summary, key: string) => client.post<ApiResponse<Draft>>(`${base}/drafts`, {
    base_release_id: summary.active_release_id, expected_head_version: summary.head_version,
  }, { headers: headers(key) }).then(data),
  patch: (draft: Draft, operations: Operation[], key: string, proof?: string) => client.patch<ApiResponse<Draft>>(
    `${base}/drafts/${draft.id}`, { operations }, { headers: headers(key, draft.etag, proof) }).then(data),
  rebase: (draft: Draft, head: number, resolutions: RebaseResolution[], key: string) => client.post<ApiResponse<Draft>>(
    `${base}/drafts/${draft.id}/rebase`, { expected_head_version: head, resolutions }, { headers: headers(key, draft.etag) }).then(data),
  reauth: (password: string) => client.post<ApiResponse<{ proof: string }>>('/api/v1/auth/reauth', { password }).then(data),
  createPolicy: (policy: PolicyInput, key: string, proof: string) => client.post<ApiResponse<Policy>>(
    `${base}/endpoint-policies`, policy, { headers: headers(key, undefined, proof) }).then(data),
  test: (draft: Draft, role: RoleKey, key: string, cold: boolean, fallback?: RoleKey) => client.post<ApiResponse<Validation>>(
    `${base}/drafts/${draft.id}/test`, { role_key: role, expected_draft_version: draft.draft_version,
      budget: cold ? 'local_cold' : 'standard', ...(fallback ? { fallback_role_key: fallback } : {}) },
    { timeout: cold ? 100000 : 40000, headers: headers(key, draft.etag) }).then(data),
  validation: (id: string, signal?: AbortSignal) => client.get<ApiResponse<Validation>>(`${base}/validations/${id}`, { signal }).then(data),
  publish: (draft: Draft, summary: Summary, key: string, proof: string) => client.post<ApiResponse<{ release_id: string; head_version: number; release_number: number }>>(
    `${base}/drafts/${draft.id}/publish`, { expected_draft_version: draft.draft_version, expected_head_version: summary.head_version },
    { headers: headers(key, draft.etag, proof) }).then(data),
  releases: (signal?: AbortSignal) => client.get<ApiResponse<Page<Release>>>(`${base}/releases`, { signal }).then(data),
  restore: (release: Release, summary: Summary, key: string, proof: string) => client.post<ApiResponse<{ release_id: string; head_version: number }>>(
    `${base}/releases/${release.id}/restore`, { expected_head_version: summary.head_version }, { headers: headers(key, undefined, proof) }).then(data),
  catchUpRelease: (release: Release, summary: Summary, key: string, proof: string) => client.post<ApiResponse<IndexCatchUp>>(
    `${base}/releases/${release.id}/index-catch-up`, { expected_head_version: summary.head_version },
    { headers: headers(key, undefined, proof) }).then(data),
  indexCatchUp: (id: string, signal: AbortSignal) => client.get<ApiResponse<IndexCatchUp>>(
    `${base}/embedding-migrations/${id}`, { signal }).then(data),
};

export function configError(error: unknown): { message: string; conflict: boolean } {
  const response = (error as { response?: { status?: number; data?: { code?: number; message?: string; data?: { reason?: string } } } })?.response;
  const conflict = response?.data?.code === 40921 || response?.data?.code === 40920;
  const reason = response?.data?.data?.reason;
  const reasons: Record<string, string> = {
    INDEX_BASELINE_NOT_VERIFIED: '请先核验并绑定知识库与笔记索引，再测试和发布。缺少构建证据时请保持向量角色停用。',
    QUERY_SPACE_UNVERIFIED: '查询模型连接已变化，请由管理员重新核验空间来源。',
    INDEX_MIGRATION_REQUIRED: '请先构建并登记独立候选索引，再发布迁移草稿。',
    INDEX_MIGRATION_REQUIRES_ISOLATED_COLLECTION: '候选索引和已使用的集合重合，请登记一个独立集合。',
    INDEX_BUILD_NOT_READY: '候选索引仍在构建或追平。任务就绪后可发布。',
    INDEX_RESTORE_CATCH_UP_RUNNING: '历史索引正在追平。当前写入批次结束后可重试恢复。',
    INDEX_SWITCH_NOT_CAUGHT_UP: '目标索引与当前数据不一致。请追平新增、编辑和删除后重试。',
    INDEX_SWITCH_JOBS_PENDING: '仍有索引更新或清理任务，处理完毕后可重试切换。',
    INDEX_SWITCH_COLLECTION_UNVERIFIED: '目标集合无法核验。请检查集合身份、维度和距离度量。',
    INDEX_QUERY_PROFILE_NOT_SELECTED: '索引的查询连接与当前向量角色不一致，请同步保存绑定后重新测试。',
    FALLBACK_RUNTIME_PENDING: '跨角色备用模型尚未支持发布，请先清空回退列表。',
    FALLBACK_CAPABILITY_UNVERIFIED: '请在来源角色的能力测试区测试备用模型，再发布草稿。',
    FALLBACK_EGRESS_MISMATCH: '主备模型须使用同一协议。跨服务切换要求主备端点处于同一网络范围，且各自的端点策略均明确允许。',
    FALLBACK_CREDENTIAL_MISMATCH: '主模型和备用模型需要使用同一 API Key，或均使用无认证连接。',
    FALLBACK_CONTEXT_MISMATCH: '请设置主模型和备用模型的上下文窗口。备用模型窗口应至少与主模型相同，输出上限和计数策略需一致。',
    MODEL_OUTPUT_EXCEEDS_CONTEXT: '上下文窗口必须大于最大输出 Token。',
    FALLBACK_STATIC_ROLE_REQUIRED: '请选择已启用、固定配置且没有备用模型的角色。',
    FALLBACK_SINGLE_CHAT_MODEL_REQUIRED: '固定配置的聊天角色可以设置一个备用模型。',
    FALLBACK_DISTINCT_MODEL_REQUIRED: '请选择使用不同模型的角色作为备用。',
    PROMPT_REVISION_REQUIRES_RESAVE: '提示词契约已升级，请重新保存该角色提示词并测试。',
    PROMPT_VARIABLE_UNSUPPORTED: '提示词包含该角色不支持的模板变量，请核对页面列出的变量。',
    CREDENTIAL_REQUIRED: '此模型需要 API Key，请选择替换密钥并填写新 Key。',
    CREDENTIAL_ORIGIN_MISMATCH: '服务地址已改变，请重新配置该地址对应的密钥。',
    ENDPOINT_DENIED: '模型地址不符合所选端点策略，请核对服务地址与路径。',
    RESOURCE_ACTION_IN_PROGRESS: '此角色已有测试正在进行，请稍后读取原测试结果。',
    ROLE_BINDING_CYCLE: '模型跟随或降级关系存在循环，请调整后再保存。',
    PROBE_STATIC_PROFILE_REQUIRED: '请先保存独立模型，或绑定到已配置的角色后再测试。',
    OUTBOX_STATE_CHANGED: '此事件状态或次数已变化，请刷新通知后核对。',
    OUTBOX_ALREADY_REDRIVEN: '此死信已创建补投事件，请刷新并查看补投状态。',
    OUTBOX_EVENT_INVALID: '事件记录不完整，无法补投，请保留记录并检查控制存储。',
    OUTBOX_TARGET_UNAVAILABLE: '此通知引用的凭据已清理或状态已变化，无法补投；原记录保留。',
  };
  if (conflict) return { message: '草稿或发布版本已变化。你的未保存内容已保留，请先重新读取并核对差异。', conflict };
  if (reason && reasons[reason]) return { message: reasons[reason], conflict: false };
  if (response?.status === 403) return { message: '此操作需要相应的模型配置权限，或账户已被停用。', conflict: false };
  if (response?.data?.code === 40110 || response?.data?.code === 40111) return { message: '重新认证已失效，请再次保存并验证密码。', conflict: false };
  if (response?.data?.code === 50303) return { message: '配置存储或主密钥不可用，请检查本地初始化与服务状态。', conflict: false };
  return { message: response?.data?.message ?? '请求未完成，请检查网络后重试。', conflict: false };
}
