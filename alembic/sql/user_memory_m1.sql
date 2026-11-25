
CREATE TABLE user_memory_heads (
  user_id VARCHAR(36) NOT NULL,
  active_revision_id VARCHAR(36) NULL,
  last_revision_number BIGINT UNSIGNED NOT NULL DEFAULT 0,
  head_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
  memory_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  auto_extract BOOLEAN NOT NULL DEFAULT TRUE,
  management_mode VARCHAR(16) NOT NULL DEFAULT 'auto',
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CHECK (management_mode IN ('auto','manual')),
  CHECK (enabled IN (0,1) AND auto_extract IN (0,1)),
  PRIMARY KEY (user_id),
  CONSTRAINT fk_um_head_owner FOREIGN KEY (user_id) REFERENCES users (uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE user_memory_revisions (
  id VARCHAR(36) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  revision_number BIGINT UNSIGNED NOT NULL,
  content_md MEDIUMTEXT NOT NULL,
  content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_kind VARCHAR(24) NOT NULL,
  parent_revision_id VARCHAR(36) NULL,
  source_request_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
  created_at DATETIME(6) NOT NULL,
  CHECK (revision_number > 0),
  CHECK (OCTET_LENGTH(content_md) <= 32768),
  CHECK (source_kind IN ('auto_init','model_generation','user_edit','proposal_accept','restore','clear')),
  PRIMARY KEY (id),
  UNIQUE KEY uq_um_revision_owner (user_id,id),
  UNIQUE KEY uq_um_revision_number (user_id,revision_number),
  CONSTRAINT fk_um_revision_head FOREIGN KEY (user_id) REFERENCES user_memory_heads (user_id),
  CONSTRAINT fk_um_revision_parent FOREIGN KEY (user_id,parent_revision_id)
    REFERENCES user_memory_revisions (user_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE user_memory_heads ADD CONSTRAINT fk_um_head_revision
  FOREIGN KEY (user_id,active_revision_id) REFERENCES user_memory_revisions (user_id,id);

CREATE TABLE user_memory_jobs (
  id VARCHAR(36) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  source_request_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_message_id INT NULL,
  source_snapshot JSON NULL,
  base_revision_id VARCHAR(36) NULL,
  expected_head_version BIGINT UNSIGNED NOT NULL,
  memory_epoch BIGINT UNSIGNED NOT NULL,
  scope_type VARCHAR(16) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  config_release_id VARCHAR(36) NOT NULL,
  generation_number INT UNSIGNED NOT NULL DEFAULT 1,
  status VARCHAR(24) NOT NULL,
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  lease_owner VARCHAR(36) NULL,
  lease_until DATETIME(6) NULL,
  deadline_at DATETIME(6) NOT NULL,
  proposal_expires_at DATETIME(6) NULL,
  proposed_content_md MEDIUMTEXT NULL,
  proposed_content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  applied_revision_id VARCHAR(36) NULL,
  error_reason VARCHAR(64) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CHECK (attempt_count <= 2),
  CHECK (generation_number > 0),
  CHECK (OCTET_LENGTH(proposed_content_md) <= 32768),
  CHECK (status IN ('queued','running','proposed','applied','no_change','failed','obsolete','cancelled','skipped_limit')),
  PRIMARY KEY (id),
  UNIQUE KEY uq_um_job_source (user_id,source_request_id),
  UNIQUE KEY uq_um_job_owner (user_id,id),
  KEY ix_um_job_poll (status,lease_until,created_at),
  CONSTRAINT fk_um_job_head FOREIGN KEY (user_id) REFERENCES user_memory_heads (user_id),
  CONSTRAINT fk_um_job_base FOREIGN KEY (user_id,base_revision_id)
    REFERENCES user_memory_revisions (user_id,id),
  CONSTRAINT fk_um_job_applied FOREIGN KEY (user_id,applied_revision_id)
    REFERENCES user_memory_revisions (user_id,id),
  CONSTRAINT fk_um_job_release FOREIGN KEY (scope_type,scope_id,config_release_id)
    REFERENCES ai_config_releases (scope_type,scope_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE user_memory_requests (
  id VARCHAR(36) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  action VARCHAR(24) NOT NULL,
  request_key VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  hash_key_version VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  result_metadata JSON NULL,
  created_at DATETIME(6) NOT NULL,
  replay_until DATETIME(6) NOT NULL,
  tombstone_until DATETIME(6) NOT NULL,
  CHECK (action IN ('save','settings','restore','generate','apply','dismiss','clear')),
  CHECK (status IN ('succeeded','failed','expired')),
  PRIMARY KEY (id),
  UNIQUE KEY uq_um_request (user_id,action,request_key),
  CONSTRAINT fk_um_request_owner FOREIGN KEY (user_id) REFERENCES users (uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

