-- 附件 B / v1.2 / 2026-11-15
-- MySQL 8.0.16+ DDL 草案，不是 Alembic 迁移；隔离执行范围与结果见附件 06。
-- M0: tool_call_audit ALTER 单独迁移；M1: 新表（除标注 M3 者）与 model_traces ALTER。
-- 本轮在工作区隔离 MySQL 8 执行的结果见附件 06；不代表 Alembic 正反向或真实旧库迁移通过。
-- M3: 最后的 embedding_migration_jobs。M2 逻辑模型见文件末尾。
-- 统一使用 (scope_type, scope_id)，系统值 ('system', 'system')；M1 不开放 user/org 输入。
-- ID 为服务端 UUID，统一 VARCHAR(36)；业务 request_id 为 32 位 hex，区别于请求记录 UUID。
-- 时间使用 UTC；目标旧库必须实测 information_schema.COLUMNS 的 charset/collation，
-- 本草案按当前 Compose 的 utf8mb4_unicode_ci。旧库不同则先 ADR/迁移，不直接强行改旧库。
-- 状态字典以附件 C4 为准；封闭枚举 CHECK 与之同步。
-- ai_config_requests.action_payload 只含固定动作参数/不可变引用，不含明文 Key 或原始 Token。
-- target_hash = SHA-256(JCS(target_spec))，hash_algorithm='jcs-sha256-v1'；target_spec 字段见 C3。

CREATE TABLE ai_version_counters (
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  entity_kind VARCHAR(32) NOT NULL,
  entity_key VARCHAR(128) NOT NULL,
  last_num BIGINT UNSIGNED NOT NULL DEFAULT 0,
  updated_at DATETIME(6) NOT NULL,
  CHECK (entity_kind IN ('profile','prompt','credential','index','endpoint_policy','price')),
  PRIMARY KEY (scope_type, scope_id, entity_kind, entity_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- revision 通过同作用域 counter 行锁递增，禁止 SELECT MAX+1。
CREATE TABLE ai_config_admin_grants (
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  subject_user_id VARCHAR(36) NOT NULL,
  config_role_key VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL,
  granted_by VARCHAR(36) NOT NULL,
  granted_at DATETIME(6) NOT NULL,
  revoked_at DATETIME(6) NULL,
  CHECK (config_role_key IN ('config_reader','config_operator','config_admin')),
  CHECK (status IN ('active','revoked')),
  PRIMARY KEY (scope_type, scope_id, subject_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- M1 角色与权限矩阵由服务端注册；grant 变更和审计在同一事务。
-- 受控初始化按 users.uuid 验证用户真实存在，不按用户名判断管理员。
-- 与 users 的物理外键需迁移时确认字符集一致；当前草案不假定旧库排序规则。
CREATE TABLE ai_config_authorization_heads (
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  policy_version BIGINT UNSIGNED NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (scope_type, scope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ai_endpoint_policies (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  policy_key VARCHAR(64) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  origin VARCHAR(320) NOT NULL,
  origin_hash CHAR(64) NOT NULL,
  allowed_path_prefix VARCHAR(512) NOT NULL,
  address_mode VARCHAR(24) NOT NULL,
  connection_rules JSON NOT NULL,
  environment VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (status IN ('active','revoked')),
  CHECK (address_mode IN ('public_https','trusted_private')),
  UNIQUE KEY uq_endpoint_scope_id (scope_type, scope_id, id),
  UNIQUE KEY uq_endpoint_revision (scope_type, scope_id, policy_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- endpoint 策略内容不可变；撤销可改 status。origin 是规范化 scheme/host/port，
-- connection_rules 含允许 IP/CIDR、DNS/重定向、代理/TLS、并发/响应上限；禁止元数据地址。
CREATE TABLE ai_secrets (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  credential_key VARCHAR(64) NOT NULL,
  credential_version BIGINT UNSIGNED NOT NULL,
  allowed_origin VARCHAR(320) NOT NULL,
  ciphertext MEDIUMBLOB NOT NULL,
  key_version VARCHAR(64) NOT NULL,
  masked_suffix VARCHAR(8) NULL,
  status VARCHAR(16) NOT NULL,
  origin_draft_id VARCHAR(36) NOT NULL,
  candidate_expires_at DATETIME(6) NULL,
  retired_at DATETIME(6) NULL,
  revocation_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  revoked_at DATETIME(6) NULL,
  KEY ix_secret_lifecycle (status, candidate_expires_at, retired_at),
  CHECK (status IN ('candidate','active','retired','revoked')),
  UNIQUE KEY uq_secret_scope_id (scope_type, scope_id, id),
  UNIQUE KEY uq_credential_version (scope_type, scope_id, credential_key, credential_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- replace 新增行；既有行的密文、origin、credential_version 不改。
-- candidate -> active/retired/revoked；active -> retired/revoked；revoked 不可恢复。
-- 普通 replace 不自动撤销旧行；无引用退役和清理见文件后部。
-- 主密钥轮换也生成新密文行和新发布，旧在途引用结束后按保留策略处理。
CREATE TABLE ai_profile_revisions (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  profile_key VARCHAR(64) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  display_name VARCHAR(128) NOT NULL,
  model_kind VARCHAR(16) NOT NULL,
  adapter_type VARCHAR(32) NOT NULL,
  adapter_version VARCHAR(64) NOT NULL,
  api_protocol VARCHAR(32) NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  base_url VARCHAR(2048) NOT NULL,
  model VARCHAR(128) NOT NULL,
  model_revision VARCHAR(255) NULL,
  auth_mode VARCHAR(24) NOT NULL,
  credential_revision_id VARCHAR(36) NULL,
  endpoint_policy_revision_id VARCHAR(36) NOT NULL,
  context_window INT UNSIGNED NULL,
  tokenizer_id VARCHAR(128) NULL,
  tokenizer_revision VARCHAR(128) NULL,
  token_counting_policy JSON NOT NULL,
  max_output_tokens INT UNSIGNED NULL,
  connect_timeout_ms INT UNSIGNED NOT NULL,
  request_timeout_ms INT UNSIGNED NOT NULL,
  embedding_dimension INT UNSIGNED NULL,
  semantic_fingerprint CHAR(64) NULL,
  semantic_identity JSON NULL,
  parameters JSON NOT NULL,
  capabilities JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  source_draft_id VARCHAR(36) NOT NULL,
  status VARCHAR(16) NOT NULL,
  retired_at DATETIME(6) NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (status IN ('candidate','published','retired')),
  CHECK (model_kind IN ('chat','embedding')),
  CHECK (auth_mode IN ('none','bearer','dashscope_key')),
  CHECK (adapter_type IN ('openai_compatible','ollama_native','dashscope_native')),
  CHECK (api_protocol IN ('chat_completions','responses','openai_embeddings','ollama_chat','ollama_embed','dashscope_embed')),
  KEY ix_profile_lifecycle (source_draft_id, status, retired_at),
  UNIQUE KEY uq_profile_scope_id (scope_type, scope_id, id),
  UNIQUE KEY uq_profile_revision (scope_type, scope_id, profile_key, revision),
  CONSTRAINT fk_profile_credential FOREIGN KEY (scope_type, scope_id, credential_revision_id)
    REFERENCES ai_secrets (scope_type, scope_id, id),
  CONSTRAINT fk_profile_endpoint FOREIGN KEY (scope_type, scope_id, endpoint_policy_revision_id)
    REFERENCES ai_endpoint_policies (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ai_prompt_revisions (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  prompt_key VARCHAR(64) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  system_text MEDIUMTEXT NOT NULL,
  template_text MEDIUMTEXT NOT NULL,
  variable_schema JSON NOT NULL,
  output_contract JSON NOT NULL,
  safety_policy_version VARCHAR(64) NOT NULL,
  content_hash CHAR(64) NOT NULL,
  source VARCHAR(32) NOT NULL,
  source_default_version VARCHAR(64) NULL,
  restored_from_revision_id VARCHAR(36) NULL,
  status VARCHAR(16) NOT NULL,
  source_draft_id VARCHAR(36) NOT NULL,
  retired_at DATETIME(6) NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (status IN ('candidate','published','retired')),
  CHECK (source IN ('import','user','default_restore')),
  KEY ix_prompt_lifecycle (source_draft_id, status, retired_at),
  UNIQUE KEY uq_prompt_scope_id (scope_type, scope_id, id),
  UNIQUE KEY uq_prompt_revision (scope_type, scope_id, prompt_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- source: import/user/default_restore；恢复默认创建新 revision，不修改历史或直接跟随文件。
CREATE TABLE rag_index_versions (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  index_key VARCHAR(64) NOT NULL,
  index_version BIGINT UNSIGNED NOT NULL,
  collection_name VARCHAR(128) NOT NULL,
  build_profile_revision_id VARCHAR(36) NULL,
  semantic_fingerprint CHAR(64) NULL,
  semantic_identity JSON NOT NULL,
  dimension INT UNSIGNED NOT NULL,
  parser_version VARCHAR(64) NOT NULL,
  chunker_version VARCHAR(64) NOT NULL,
  source_watermark VARCHAR(128) NULL,
  status VARCHAR(24) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (status IN ('baseline_verified','baseline_unverified','ready','retired','building','catching_up','failed')),
  UNIQUE KEY uq_index_scope_id (scope_type, scope_id, id),
  UNIQUE KEY uq_index_version (scope_type, scope_id, index_key, index_version),
  CONSTRAINT fk_index_build_profile FOREIGN KEY (scope_type, scope_id, build_profile_revision_id)
    REFERENCES ai_profile_revisions (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- M1 只有 baseline_verified/baseline_unverified/ready/retired；不提供重建任务。
-- M3 增 building/catching_up/failed；build_profile_revision_id 用于追溯，
-- 实际查询凭据随 release.index_bindings 中的 query_profile_revision_id 解析。
CREATE TABLE ai_config_drafts (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  base_release_id VARCHAR(36) NULL,
  base_head_version BIGINT UNSIGNED NOT NULL,
  draft_version BIGINT UNSIGNED NOT NULL,
  payload JSON NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  status VARCHAR(24) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CHECK (status IN ('editing','validating','ready','published','abandoned','expired')),
  KEY ix_draft_list (scope_type, scope_id, status, updated_at),
  KEY ix_draft_expiry (status, expires_at),
  UNIQUE KEY uq_draft_scope_id (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- payload 只含凭据引用；输入的明文 Key 加密后不保留在 payload。
CREATE TABLE ai_config_validations (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  request_record_id VARCHAR(36) NOT NULL,
  created_by VARCHAR(36) NOT NULL,
  draft_id VARCHAR(36) NOT NULL,
  draft_version BIGINT UNSIGNED NOT NULL,
  role_key VARCHAR(64) NOT NULL,
  target_hash CHAR(64) NOT NULL,
  target_spec JSON NOT NULL,
  hash_algorithm VARCHAR(32) NOT NULL,
  endpoint_policy_revision_id VARCHAR(36) NULL,
  safety_policy_version VARCHAR(64) NOT NULL,
  adapter_version VARCHAR(64) NOT NULL,
  credential_revision_id VARCHAR(36) NULL,
  credential_version BIGINT UNSIGNED NULL,
  validation_kind VARCHAR(24) NOT NULL,
  status VARCHAR(24) NOT NULL,
  result JSON NULL,
  max_calls INT UNSIGNED NOT NULL,
  max_output_tokens INT UNSIGNED NOT NULL,
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  lease_owner VARCHAR(128) NULL,
  deadline_at DATETIME(6) NOT NULL,
  lease_expires_at DATETIME(6) NULL,
  tested_at DATETIME(6) NULL,
  valid_until DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (status IN ('pending','running','passed','failed','unverified','cancelled')),
  KEY ix_validation_request (request_record_id),
  KEY ix_validation_lease (status, lease_expires_at),
  KEY ix_validation_validity (status, valid_until),
  KEY ix_validation_target (draft_id, draft_version, role_key, target_hash),
  CONSTRAINT fk_validation_draft FOREIGN KEY (scope_type, scope_id, draft_id)
    REFERENCES ai_config_drafts (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ai_config_requests (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  actor_id VARCHAR(36) NOT NULL,
  action VARCHAR(48) NOT NULL,
  request_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  actor_access_jti VARCHAR(64) NULL,
  authorization_policy_version BIGINT UNSIGNED NULL,
  reauth_verified_at DATETIME(6) NULL,
  authorization_valid_until DATETIME(6) NULL,
  action_payload JSON NOT NULL,
  execution_state JSON NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  payload_hash_key_version VARCHAR(64) NOT NULL,
  status VARCHAR(24) NOT NULL,
  result_ref VARCHAR(36) NULL,
  response_http_status SMALLINT UNSIGNED NULL,
  response_json JSON NULL,
  lease_owner VARCHAR(128) NULL,
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  lease_expires_at DATETIME(6) NULL,
  deadline_at DATETIME(6) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  completed_at DATETIME(6) NULL,
  replay_until DATETIME(6) NOT NULL,
  CHECK (status IN ('pending','running','succeeded','failed','expired')),
  UNIQUE KEY uq_request_scope_id (scope_type, scope_id, id),
  KEY ix_request_status_lease (status, lease_expires_at),
  KEY ix_request_replay (scope_type, scope_id, replay_until),
  UNIQUE KEY uq_config_request (scope_type, scope_id, actor_id, action, request_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 请求幂等记录先占位；网络探测不能放在长事务里。租约失效后按持久结果核对，
-- 不能盲重做已提交发布；默认 7 天重放窗口，保留历史键摘要和 result_ref 供审计。
-- 异步任务只记录服务端已验证的身份/时限，不保存 Bearer 或 reauth token；提交前重查
-- 账户、grant、jti 撤销和 reauth 时效。发布与 grant 变更以授权 head 锁/版本比较排序。
CREATE TABLE ai_config_releases (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  release_number BIGINT UNSIGNED NOT NULL,
  draft_id VARCHAR(36) NOT NULL,
  draft_version BIGINT UNSIGNED NOT NULL,
  restore_from_release_id VARCHAR(36) NULL,
  request_record_id VARCHAR(36) NOT NULL,
  schema_version INT UNSIGNED NOT NULL,
  role_bindings_raw JSON NOT NULL,
  resolved_static_bindings JSON NOT NULL,
  follow_execution_roles JSON NOT NULL,
  index_bindings JSON NOT NULL,
  validation_refs JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_release_scope_id (scope_type, scope_id, id),
  UNIQUE KEY uq_release_number (scope_type, scope_id, release_number),
  UNIQUE KEY uq_release_draft (draft_id),
  UNIQUE KEY uq_release_request (request_record_id),
  CONSTRAINT fk_release_draft FOREIGN KEY (scope_type, scope_id, draft_id)
    REFERENCES ai_config_drafts (scope_type, scope_id, id),
  CONSTRAINT fk_release_request FOREIGN KEY (scope_type, scope_id, request_record_id)
    REFERENCES ai_config_requests (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ai_config_heads (
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  active_release_id VARCHAR(36) NULL,
  head_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
  last_release_number BIGINT UNSIGNED NOT NULL DEFAULT 0,
  manager_owner_id VARCHAR(64) NULL,
  manager_generation BIGINT UNSIGNED NOT NULL DEFAULT 0,
  manager_lease_expires_at DATETIME(6) NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (scope_type, scope_id),
  CONSTRAINT fk_head_release FOREIGN KEY (scope_type, scope_id, active_release_id)
    REFERENCES ai_config_releases (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 单一发布事务：固定锁顺序 authorization_head -> draft -> config_head -> 引用版本，
-- 对 head 行 SELECT FOR UPDATE，next_release_number=last_release_number+1，
-- 新建 release 后单条 CAS 同时更新 active_release_id/head_version/last_release_number；
-- affected_rows=0 必须回滚并返回 40920。release_number 不从 ai_version_counters 分配。
-- 核对 draft_version、权限、验证引用和 head_version -> 新建 release ->
-- CAS head 并递增 head_version/release_number -> draft published -> 请求结果、审计、Outbox。
-- JSON 中所有 ID 由服务端做同作用域/角色/凭据一致性校验，禁止只依赖 JSON 格式正确。
CREATE TABLE ai_config_audits (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  actor_id VARCHAR(36) NOT NULL,
  action VARCHAR(48) NOT NULL,
  target_key VARCHAR(128) NOT NULL,
  release_id VARCHAR(36) NULL,
  head_version_before BIGINT UNSIGNED NULL,
  head_version_after BIGINT UNSIGNED NULL,
  credential_revision_id VARCHAR(36) NULL,
  credential_version BIGINT UNSIGNED NULL,
  restore_from_release_id VARCHAR(36) NULL,
  result VARCHAR(24) NOT NULL,
  redacted_diff JSON NOT NULL,
  request_id VARCHAR(32) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  KEY ix_config_audit (scope_type, scope_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE outbox_events (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  event_key VARCHAR(128) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  aggregate_id VARCHAR(128) NOT NULL,
  aggregate_version BIGINT UNSIGNED NOT NULL,
  payload JSON NOT NULL,
  status VARCHAR(24) NOT NULL,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  next_attempt_at DATETIME(6) NOT NULL,
  lease_owner VARCHAR(128) NULL,
  lease_expires_at DATETIME(6) NULL,
  error_code VARCHAR(64) NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (status IN ('pending','inflight','succeeded','retry','dead_letter')),
  UNIQUE KEY uq_outbox_event (scope_type, scope_id, event_key),
  KEY ix_outbox_poll (status, next_attempt_at, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- M1 消费者是受控同进程 poller：处理 config.published / credential.revoked / grant.changed，
-- 预热和本地取消为尽力投递；审计事实在发布事务内同步提交，不依赖本消费者。
-- M2 可加 Redis 通知/独立消费者；M3 加 resource.index_requested。默认最多 5 次退避重试，
-- 失败保留 dead_letter 与告警、支持重投；业务正确性不依赖通知必达。
CREATE TABLE model_price_versions (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  origin VARCHAR(320) NOT NULL,
  model VARCHAR(128) NOT NULL,
  price_version BIGINT UNSIGNED NOT NULL,
  currency CHAR(3) NOT NULL,
  unit_tokens INT UNSIGNED NOT NULL,
  input_unit_price DECIMAL(20,10) NOT NULL,
  output_unit_price DECIMAL(20,10) NOT NULL,
  effective_from DATETIME(6) NOT NULL,
  provenance VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_price_version (scope_type, scope_id, origin, model, price_version),
  CHECK (unit_tokens > 0 AND input_unit_price >= 0 AND output_unit_price >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 以下 ALTER 是 M1 目标，迁移代码必须 inspect 现有表，不直接重复执行本文件。
ALTER TABLE model_traces
  ADD COLUMN release_id VARCHAR(36) NULL,
  ADD COLUMN profile_revision_id VARCHAR(36) NULL,
  ADD COLUMN prompt_revision_id VARCHAR(36) NULL,
  ADD COLUMN credential_revision_id VARCHAR(36) NULL,
  ADD COLUMN credential_version BIGINT UNSIGNED NULL,
  ADD COLUMN price_version_id VARCHAR(36) NULL,
  ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN adapter_type VARCHAR(32) NULL,
  ADD COLUMN provider_origin VARCHAR(320) NULL,
  ADD COLUMN workflow_id VARCHAR(36) NULL,
  ADD COLUMN step_id VARCHAR(36) NULL,
  ADD COLUMN usage_status VARCHAR(24) NULL,
  ADD COLUMN cost_amount DECIMAL(20,10) NULL,
  ADD COLUMN cost_currency CHAR(3) NULL,
  ADD COLUMN pricing_status VARCHAR(32) NULL,
  ADD CONSTRAINT chk_trace_usage_status CHECK (usage_status IS NULL OR usage_status IN ('known','unknown','missing')),
  ADD CONSTRAINT chk_trace_pricing_status CHECK (pricing_status IS NULL OR pricing_status IN ('priced','unknown','legacy_estimate'));

-- phase: M0。仅本 ALTER 归 M0；先于 M0-07 验收，与 model_traces 的 M1 ALTER 分开。
-- 复用工具审计表，新增精确匹配；不新造另一份同义审计表。
ALTER TABLE tool_call_audit
  ADD COLUMN run_id VARCHAR(128) NULL,
  ADD COLUMN tool_call_id VARCHAR(128) NULL,
  ADD COLUMN step_id VARCHAR(36) NULL,
  ADD COLUMN operation_id VARCHAR(36) NULL,
  ADD COLUMN error_code VARCHAR(64) NULL;


-- M1：多态引用由同事务注册器校验对象存在/作用域/类型；保留外键保护草稿和发布本身。
-- 清理以本索引反查，不能只扫描 JSON；JSON 与引用索引同事务更新，失败整体回滚。
CREATE TABLE ai_config_draft_items (
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  draft_id VARCHAR(36) NOT NULL,
  entity_kind VARCHAR(24) NOT NULL,
  entity_id VARCHAR(36) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (scope_type, scope_id, draft_id, entity_kind, entity_id),
  KEY ix_draft_item_entity (scope_type, scope_id, entity_kind, entity_id),
  CHECK (entity_kind IN ('profile','prompt','credential','endpoint_policy','index')),
  CONSTRAINT fk_draft_item_draft FOREIGN KEY (scope_type, scope_id, draft_id)
    REFERENCES ai_config_drafts (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ai_config_release_refs (
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  release_id VARCHAR(36) NOT NULL,
  entity_kind VARCHAR(24) NOT NULL,
  entity_id VARCHAR(36) NOT NULL,
  PRIMARY KEY (scope_type, scope_id, release_id, entity_kind, entity_id),
  KEY ix_release_ref_entity (scope_type, scope_id, entity_kind, entity_id),
  CHECK (entity_kind IN ('profile','prompt','credential','endpoint_policy','index')),
  CONSTRAINT fk_release_ref_release FOREIGN KEY (scope_type, scope_id, release_id)
    REFERENCES ai_config_releases (scope_type, scope_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 表创建完成后增加来源/请求外键，避免前向引用。
ALTER TABLE ai_secrets ADD CONSTRAINT fk_secret_origin_draft
  FOREIGN KEY (scope_type, scope_id, origin_draft_id)
  REFERENCES ai_config_drafts (scope_type, scope_id, id);
ALTER TABLE ai_profile_revisions ADD CONSTRAINT fk_profile_source_draft
  FOREIGN KEY (scope_type, scope_id, source_draft_id)
  REFERENCES ai_config_drafts (scope_type, scope_id, id);
ALTER TABLE ai_prompt_revisions ADD CONSTRAINT fk_prompt_source_draft
  FOREIGN KEY (scope_type, scope_id, source_draft_id)
  REFERENCES ai_config_drafts (scope_type, scope_id, id);
ALTER TABLE ai_config_validations ADD CONSTRAINT fk_validation_request
  FOREIGN KEY (scope_type, scope_id, request_record_id)
  REFERENCES ai_config_requests (scope_type, scope_id, id);
ALTER TABLE ai_config_validations ADD CONSTRAINT fk_validation_endpoint
  FOREIGN KEY (scope_type, scope_id, endpoint_policy_revision_id)
  REFERENCES ai_endpoint_policies (scope_type, scope_id, id);

-- 清理与不可变控制：
-- candidate 凭据仅供所属未结束草稿的测试；真实推理只准 active 或已固定在途许可。
-- 发布事务提升被引用的 candidate -> active / profile,prompt -> published，并登记 release_refs。
-- 草稿 abandoned/expired 后作业在 24h 内按 draft_items 标 retired，保留 ID/审计；
-- 无任意 release_refs、未结束草稿、有效验证租约或在途引用时，7 天后先删派生 profile/prompt，
-- 再删无引用候选凭据行（含密文）与 draft_items；必要的验证凭据引用仅保留 ID/脱敏证据。
-- 不以永久 RESTRICT 异常代替清理；引用注册、退役及清理分别有事务/重试/审计和测试。
-- 生产业务账号对不可变内容只授 SELECT/INSERT；控制列采用列级 UPDATE：
-- secrets 仅 status/revocation_epoch/revoked_at/candidate_expires_at/retired_at；
-- profiles/prompts 仅 status/retired_at；endpoint_policies 仅 status。
-- 内容/密文原地 UPDATE、任意 DELETE 禁止；维护账号只在引用检查后的清理流程使用。

-- M3 才创建：M1 只读索引状态，不声称支持后台迁移。
CREATE TABLE embedding_migration_jobs (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  index_key VARCHAR(64) NOT NULL,
  from_index_version_id VARCHAR(36) NOT NULL,
  to_index_version_id VARCHAR(36) NOT NULL,
  target_profile_revision_id VARCHAR(36) NOT NULL,
  stage VARCHAR(32) NOT NULL,
  status VARCHAR(24) NOT NULL,
  processed_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
  total_count BIGINT UNSIGNED NULL,
  source_watermark VARCHAR(128) NULL,
  applied_watermark VARCHAR(128) NULL,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  lease_owner VARCHAR(128) NULL,
  lease_expires_at DATETIME(6) NULL,
  error_code VARCHAR(64) NULL,
  CHECK (status IN ('queued','building','catching_up','validating','ready_to_switch','switched','failed','cancelled')),
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- M2 最小逻辑模型，复用既有审查 §6 命名；实现阶段另建 Alembic，不在 M1 偷建：
-- workflow_runs(id, scope_type, scope_id, subject_id, release_id, status, deadline,
--   budget, state_version, checkpoint_ref, created_at, updated_at)
-- agent_tasks(id, workflow_id, parent_id, role_key, status, input_refs, result_refs,
--   attempt, lease_expires_at, error_code)
-- task_events(task_id, sequence, type, payload, created_at), UNIQUE(task_id, sequence)
-- tool_operations(scope_type, scope_id, operation_id, task_id, tool_call_id, args_hash,
--   status, result_ref, approval_id), UNIQUE(scope_type, scope_id, operation_id)
-- operation_approvals(id, operation_id, subject_id, payload_hash, expires_at, consumed_at)
-- M3 文档版本、切片、权限/活动版本和 ingestion_jobs 延续既有审查模型。
-- 聊天消息幂等沿用 chat_messages，另行迁移到用户作用域唯一约束；配置 request_key、
-- 工具 operation_id 和聊天 idempotency_key 属于不同命名空间，不能互相代用。
-- 旧 model_pricing 只导入为有 provenance 的初始报价；旧 trace 未知历史价格保留
-- legacy_estimate/unknown，禁止按当前价格伪造历史 price_version_id。
