-- 附件 F 存储扩展 / v1.3 / 2026-11-15
-- MySQL 8.0.16+，在附件 02 后执行，保留 02 的历史证据哈希。
-- 4 CREATE + 1 ALTER；不是已交付的 Alembic 迁移。
-- users.uuid VARCHAR(36)；ChatMessage.id 为 INT，来源为逻辑引用并由服务层验证归属。
-- 旧库排序规则须先核验；本文沿用 utf8mb4_unicode_ci。
-- 正文不可变，唯一例外为用户显式 purge_history 的彻底清理事务。

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

-- API 同步写事务不需要持久 running 请求行；generate 的受理与 job 插入同事务。
-- result_metadata 只留状态/版本/ID，不存正文；重放根据 result revision 受控读取。
-- 已 purge 的结果返回 41040，不从旧任务或日志恢复正文。
-- purge 顺序：锁 head 并递增 epoch -> 清空 active_revision -> 清除 job 源/候选与版本引用
-- -> 按 revision_number 倒序删除旧版本（自引用父版本）-> 新空 revision -> 移动 head。
-- 所有步骤同一事务；source_request_id/created_at 等审计元数据允许按保留策略保留。
-- generation_number 仅允许显式重试同一 source 的失败/过期任务递增，旧 Worker 必须被栅栏拒绝。
-- 命中源唯一键时不能 INSERT 新 job；显式重试重置 attempt/lease/deadline/基线并校验旧状态。
-- 用户删除先断 head 指针，清 job/request/revision 再删 head；迁移不得随意禁用外键。
