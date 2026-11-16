-- Reviewed M1 tables only; no audit ALTER or M3 job table.

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
