# 附件 C：M1 API、状态机与安全契约

2026-11-16 增量：文本配置 publish、历史 restore、固定请求快照及个人记忆领域 API 已接入，见[发布与记忆实施记录](../实施记录/M1_模型发布热更新与个人记忆_2026-11-16.md)。2026-11-17 增加 rebase API/页面与受控 CLI 的旧配置预览/迁移草稿，见[导入与合并实施记录](../实施记录/M1_旧配置导入与草稿合并_2026-11-17.md)。同日接通下述已有索引绑定与凭据紧急撤销。2026-11-18 接入单个同服务聊天备用模型，规则见 C6。未验证向量空间仍阻止发布；完整初始化导入与索引迁移管理仍为设计目标，不应推断为可调用。

版本：v1.3，2026-11-15；2026-11-16 补齐 grant/探测输入 Schema 与 memory_extraction 提示词 PATCH。本文仍是完整设计目标，已实现子集与差异见[M1 首批记录](../实施记录/M1_模型配置控制面首批实施_2026-11-16.md)及[页面与探测记录](../实施记录/M1_模型配置页面与能力探测_2026-11-16.md)，不能根据下表推断所有接口已可用。配置路径公共前缀为 /api/v1/model-config，认证、用量和个人记忆接口另标全路径。个人记忆的唯一领域 API 表在[附件 F §F6](./07_个人记忆文件功能设计.md)，前缀 /api/v1/user/memory，普通用户仅管理自己的内容，不继承模型管理员权限。[附件 A](./01_角色注册表.md)定义角色，[附件 B](./02_数据模型DDL.sql)定义存储；[附件 E JSON Schema](./05_配置契约.schema.json)是字段级类型来源，以 $defs 中指定入口校验。

2026-11-17 后续增量：embedding-status 及[待核验基线登记](../实施记录/M1_现有向量索引基线登记_2026-11-17.md)之后，新增[管理员构建记录核验、请求级索引绑定、同空间连接更新与缓存隔离](../实施记录/M1_请求级向量绑定与连接热更新_2026-11-17.md)。状态读取只访问配置库；目录登记不能自动证明来源。具备人工核验证据并通过当前版本能力测试的既有空间可发布，未知空间和新索引切换仍被拦截。索引迁移 Job 尚未开放。

2026-11-19 M3-02 接入 `attest-indexes --migration`。已独立构建并登记的候选集合继续通过原 publish、restore 路由激活。追平校验失败返回 `42220`，`data.reason` 给出 `INDEX_SWITCH_NOT_CAUGHT_UP`、`INDEX_SWITCH_JOBS_PENDING` 或 `INDEX_SWITCH_COLLECTION_UNVERIFIED` 等原因。自动迁移 Job 仍处于后续实施范围。[索引切换实施记录](../实施记录/M3_索引切换与回滚校验_2026-11-19.md)

2026-11-20 启用候选构建 Job。任务管理通过下表 POST 和 GET 接口，完成后的候选索引绑定到原草稿，并继续接受发布前校验。[后台构建实施记录](../实施记录/M3_索引切换与回滚校验_2026-11-19.md)

2026-11-17 凭据增量：GET /credentials/{id}/impact 与 POST /credentials/{id}/revoke 已接入页面，见[撤销实施记录](../实施记录/M1_凭据紧急撤销与在途取消_2026-11-17.md)。附件 E 同步新增 CredentialRevocationInput/Result，应用权限目录现有 35 条管理路由。影响读取只查脱敏配置，不解密 Key 或调用供应商；紧急撤销仅针对指定凭据版本。

2026-11-17 通知管理增量：配置管理员可读取通知列表/详情并补投死信，见[通知管理实施记录](../实施记录/M1_配置通知查询与死信补投_2026-11-17.md)。新增 config.outbox.manage，仅授予 config_admin；权限目录由 35 扩展为 38 条路由。字段 Schema 新增 OutboxRedriveInput/Result，存储沿用既有 Outbox，无新增迁移。

## C0. M0 横切响应、错误与关联标识

当前成功信封有 data，失败信封只有 detail，限流还使用 HTTPException(detail=...)；以下是 M0 必须实现的兼容变更，不冒充现状：

- failed_response/BusinessError 增加可选结构化 data，保留 detail:string|null 一个兼容版本；默认 data=null，detail 不含未经脱敏的异常。
- 通用业务异常、Pydantic 422、限流和认证均通过统一失败信封；rate_limit 改抛 BusinessError，异常处理器注入同一 request_id。
- 业务 request_id 规范为请求入口生成的 32 位小写 hex，响应/日志/model trace 使用同值；保留历史 12/36 字符标识，不回填伪造新标识。ai_config_requests.id 是独立 UUID，DTO 用 request_record_id，不能混用。
- 现有 HTTP 422 + code=40002 保留通用 Schema 校验口径；新 4222x 只用于配置领域规则。前端401自动刷新只处理现有 TOKEN_EXPIRED/TOKEN_INVALID，reauth 的 40110/40111 触发重新认证，不刷新后登出。
- 映射表是新增登记目标：保留既有编码，不按业务码前缀推导 HTTP。新码已对照当前 ErrorCode，M0 还需集成测试。

| reason / 现有名称 | code | HTTP |
| --- | --- | --- |
| TOKEN_EXPIRED / TOKEN_INVALID | 40101 / 40102（已有，禁止重用） | 401 |
| REAUTH_REQUIRED / REAUTH_INVALID | 40110 / 40111 | 401 |
| PASSWORD_ERROR / ACCOUNT_LOCKED | 40103 / 40104（已有） | 401 / 403 |
| FORMAT_VALIDATION_FAILED | 40002（已有） | 422 |
| ENDPOINT_DENIED / CONFIG_PERMISSION_DENIED / SCOPE_DENIED / ACCOUNT_INACTIVE | 40310 / 40312 / 40313 / 40314 | 403 |
| HEAD_VERSION_CONFLICT / DRAFT_VERSION_CONFLICT / IDEMPOTENCY_PAYLOAD_MISMATCH | 40920 / 40921 / 40922 | 409 |
| RESOURCE_ACTION_IN_PROGRESS | 40923 | 409（另一动作占用资源；同键同体返回原 202/结果） |
| DRAFT_EXPIRED | 40924 | GET 为 410；写为 409 |
| REBASE_CONFLICT / CREDENTIAL_REVOKED / REBASE_REQUIRED | 40925 / 40926 / 40927 | 409 |
| LAST_ADMIN_PROTECTED / REQUEST_REPLAY_EXPIRED | 40928 / 40929 | 409 / 410 |
| FEATURE_NOT_AVAILABLE | 40930 | 409 |
| EMBEDDING_SPACE_MISMATCH / TEMPLATE_VARIABLE_MISSING / CAPABILITY_UNVERIFIED | 42220 / 42221 / 42222 | 422 |
| MEMORY_VERSION_CONFLICT / MEMORY_PROPOSAL_OBSOLETE / MEMORY_JOB_IN_PROGRESS / MEMORY_IDEMPOTENCY_MISMATCH / MEMORY_EXTRACTION_UNAVAILABLE | 40940 / 40941 / 40942 / 40943 / 40944 | 409；详见 F6 |
| MEMORY_NOT_FOUND / MEMORY_RESULT_PURGED | 40440 / 41040 | 404 / 410 |
| MEMORY_CONTENT_TOO_LARGE / MEMORY_CONTENT_REJECTED / MEMORY_SOURCE_REQUIRED / MEMORY_STORE_UNAVAILABLE | 41340 / 42240 / 42241 / 50340 | 413 / 422 / 422 / 503 |
| IF_MATCH_REQUIRED | 42801 | 428 |
| GLOBAL_RATE_LIMIT / ENDPOINT_RATE_LIMIT | 42901 / 42902（已有） | 429 |
| 请求内模型预算拒绝（细因见 C6） | 42903 | JSON 为 HTTP 429，SSE 已开始时为终止 error 事件 |
| MODEL_CONTEXT_LIMIT_EXCEEDED | 40008 | JSON 为 HTTP 413，聊天 SSE 已开始时为终止 error 事件 |
| BACKGROUND_CAPACITY_EXCEEDED | 50306 | HTTP 受理前为 503；SSE 已开始则发送 error/retryable 事件，不更改 HTTP 状态 |
| CONTROL_STORE_UNAVAILABLE / MULTI_WORKER_UNSUPPORTED | 50303 / 50304 | 503 |
| MODEL_CONFIG_NOT_READY | 50305 | 503（管理 GET 仍可 200 setup_required） |
| ACTION_DEADLINE_EXCEEDED | 50410 | 504 |

错误示例；兼容窗口保留 detail，结构化原因在 data，不修改原 detail 的类型：

```json
{"code":40920,"message":"活动发布已更新，请重新建立草稿基线","detail":null,"data":{"reason":"HEAD_VERSION_CONFLICT","expected":12,"actual":13},"request_id":"0123456789abcdef0123456789abcdef"}
```

```json
{"code":42220,"message":"候选向量空间与现有索引不兼容","detail":null,"data":{"reason":"EMBEDDING_SPACE_MISMATCH","field":"/index_bindings/notes","migration_required":true},"request_id":"0123456789abcdef0123456789abcdef"}
```

## C1. 权限、单进程前提与重新认证

M1 模型配置服务端固定 scope_type=system、scope_id=system，不信任客户端作用域。个人记忆所有权独立取认证用户，不表示提前开放个人/组织模型覆盖。config_role_key 是管理权限角色，与附件 A 的业务 role_key 分开。

| 权限 | config_reader | config_operator | config_admin |
| --- | --- | --- | --- |
| config.read：脱敏配置/草稿/模板/索引状态 | 允许 | 允许 | 允许 |
| config.edit：草稿/业务模板 | 拒绝 | 允许 | 允许 |
| config.test：发现/能力测试 | 拒绝 | 允许 | 允许 |
| config.publish / config.restore | 拒绝 | 拒绝 | 允许 |
| config.audit.read | 拒绝 | 拒绝 | 允许 |
| config.outbox.manage：人工补投配置通知死信 | 拒绝 | 拒绝 | 允许 |
| config.credential.revoke / endpoint.manage / grant.manage | 拒绝 | 拒绝 | 允许 |

新增 require_config_permission 依赖：现有 get_current_user_id 验 JWT 和黑名单后，继续读 users.status、当前 grant 和 authorization_head，核对作用域、所需权限、M1 管理实例租约及必要的 reauth。M0 建立规则/响应，M1 实现该依赖；不能假定当前认证函数已经查账户状态或 grant。限流在身份设置后执行。

首次管理员用受控 CLI 指定真实 active users.uuid；授权变更锁定同一授权 head 并递增 policy_version。撤销或降级最后一个 active config_admin 返回 40928，保持 grant、head 和审计状态；应急恢复仅由受控离线管理渠道，不在 HTTP 参数中提供绕过开关。

M1 只支持一个应用进程。启动配置 workers!=1 拒绝启用并保持未就绪；同时通过 config_heads 的 manager_owner_id/manager_generation/manager_lease_expires_at 防止额外副本绕过部署声明。启动取得 30 秒管理租约，每 5 秒续约；新敏感动作和提交事务都校验 owner/generation/时效。第二进程不能同时通过就绪，旧 owner 失去租约后不能继续写。多进程仅做解析器离线模拟，正式支持在后续阶段另验收。

POST /api/v1/auth/reauth：输入附件 E 的 ReauthInput（password），需现有有效 Bearer。响应 data 为 ReauthOutput：proof、proof_type=config_reauth、expires_in=600、expires_at、actor_id、scope_type、scope_id。签名载荷绑定 actor、access_jti、scope、issued_at/exp 和签名密钥版本；不含密码。服务端只接受用途匹配的证明，经 X-Admin-Reauth 提交；前端仅存内存。独立部署签名密钥持久化，重启后仍能验证未过期证明；换 access_jti、撤权或签名版本撤销会使证明失效。M1 不因此获得多实例部署资格。

替换/清空密钥、发布、恢复、紧急撤销、grant/端点策略变更需要 reauth。密码失败复用现有登录失败计数和锁定逻辑，另对 actor/IP 施加每分钟最多 5 次、计入现有全局限流；锁定状态不被成功获取一个旧 proof 绕过。错误采用上表；不输出“账户是否存在”等额外敏感信息。

当前以显式 Bearer 身份调用，限制 Origin、JSON 和 CORS，不以 Cookie 自动提供管理身份；未来 Cookie 模式必须同时加反 CSRF token、SameSite/Secure 和 Origin 校验。供应商 Key、密码和 proof 不进入 URL/日志/浏览器持久化。

## C2. 端点表与动作恢复

下表路径均接公共前缀。创建/测试/发布/恢复/撤销/策略/grant/rebase/DELETE 使用 Idempotency-Key（1—128、区分大小写）。PATCH 需 If-Match，发布需 expected_draft_version 与 expected_head_version。所有权限都以前述现行依赖为准。

| 方法 / 路径 | 权限 | 输入与成功 |
| --- | --- | --- |
| GET / | read | head/release/effective_for/roles/index_states/permissions、current_drafts 摘要 |
| GET /roles | read | M1 的 17 个角色及 M3 的查询扩展角色，含模型类型、模板和变量 |
| GET /drafts | read | status、cursor、limit；多管理员可同时保有多个草稿，不设隐含“唯一当前草稿” |
| POST /drafts | edit | DraftCreate；201 DraftView |
| GET /drafts/{id} | read | DraftView、ETag、验证覆盖 |
| PATCH /drafts/{id} | edit | DraftPatch + If-Match；200 新 DraftView |
| DELETE /drafts/{id} | edit | If-Match；200 abandoned，启动候选退役流程 |
| POST /drafts/{id}/rebase | edit | RebaseInput + If-Match；200 新基线/版本/失效验证，冲突 40925 |
| GET /drafts/{id}/resolution | read | 静态解析绑定、动态角色、反向引用、影响角色、索引差异和验证覆盖 |
| POST /drafts/{id}/test | test | role_keys、expected_draft_version；202 request_record_id/status_url/validation_ids |
| GET /validations/{id} | read | 状态、target_spec/hash/算法/失效原因、有效期和脱敏结果 |
| POST /drafts/{id}/publish | publish + reauth | PublishInput；200 发布结果，或 202 准备动作 |
| GET /requests/{id} | read | 持久动作状态/结果/验证引用；访问仍检查 actor 和作用域授权 |
| GET /releases | read | cursor、limit；分页 |
| GET /releases/{id} | read | ReleaseView 和恢复来源 |
| POST /releases/{id}/restore | restore + reauth | RestoreInput；200/202，新建恢复草稿及发布，不修改旧清单 |
| POST /models/discover | test | draft_id、role_key、expected_draft_version；200/202；不支持发现仍可手输 |
| GET /prompts/{key}/revisions | read | cursor、limit；分页 |
| GET /prompts/{key}/revisions/{id} | read | 业务文本/变量/只读安全策略 |
| POST /drafts/{id}/prompts/{key}/restore-default | edit | If-Match；新模板修订和草稿版本，不直接发布 |
| POST /prompts/preview | read | draft_id、role_key、sample_variables；仅本地渲染 |
| GET /credentials | read | 分页脱敏凭据版本（items/next_cursor）；角色派生状态另见 DraftView.credential_states |
| GET /credentials/{id}/impact | read | 脱敏目标版本、当前启用/可能动态跟随角色、索引、历史发布与草稿引用数量；不返回 Key |
| GET /embedding-status | read | 基线/维度/查询绑定与 migration_supported=false |
| POST /credentials/{id}/revoke | credential.revoke + reauth | CredentialRevocationInput + Idempotency-Key；200 CredentialRevocationResult |
| GET /audits | audit.read | cursor、limit、action、release_id；分页脱敏 |
| GET /outbox | audit.read | status、cursor、limit；分页脱敏通知，按事件 ID 倒序 |
| GET /outbox/{id} | audit.read | 通知状态、尝试次数、时间、脱敏目标与补投事件 ID |
| POST /outbox/{id}/redrive | outbox.manage + reauth | OutboxRedriveInput + Idempotency-Key；200 OutboxRedriveResult，表示已排队 |
| GET /endpoint-policies | read | cursor、limit、status；分页可选策略 |
| GET /endpoint-policies/{id} | read | policy_key/revision/environment/address_mode、脱敏规则、引用影响 |
| POST /endpoint-policies | endpoint.manage + reauth | EndpointPolicyInput；201 新版本，不自动联网 |
| POST /endpoint-policies/{id}/revoke | endpoint.manage + reauth | reason；撤销新许可、尽力取消 |
| GET /grants | grant.manage | cursor、limit |
| PUT /grants/{user_id} | grant.manage + reauth | config_role_key、expected_policy_version |
| DELETE /grants/{user_id} | grant.manage + reauth | expected_policy_version；最后管理员保护 |
| POST /embedding-migrations | config.publish + reauth | `draft_id`、`semantic_identity`、If-Match、Idempotency-Key，返回 `202` 与任务 ID |
| GET /embedding-migrations/{id} | config.read | 任务状态、草稿版本、候选版本、水位和错误码 |
| POST /releases/{id}/index-catch-up | config.restore + reauth | `expected_head_version`、Idempotency-Key，返回 `202`、任务 ID 和历史发布 ID。复用迁移任务查询接口 |

列表统一 limit 默认 20、上限 100，cursor 不透明，返回 items、next_cursor。无数据为空数组。首次无发布时 GET / 返回 200、release_id=null、head_version=0、effective_for=none、setup_required=true，current_drafts 可发现；业务推理在 DB_CONFIG_REQUIRED 模式返回明确未配置错误。控制数据库真的不可用才返回 50303，不把“尚未配置”伪装成连接故障。

幂等命名空间为 scope + actor + action。action_payload 固定记录 draft_id、expected 版本、role_keys、恢复来源和必要资源引用，不含明文 Key、密码或 Token；敏感输入以新建凭据 ID/HMAC 表示。payload_hash 为带版本服务端 HMAC，payload_hash_key_version 指定用途隔离密钥；路径 ID 纳入规范化输入。原始重放可按原哈希版本验证，不因换 HMAC 密钥误判；旧验证材料不可用则明确拒绝而不是执行新动作。

requests 记录 action_payload、execution_state、lease_owner、attempt_count、deadline、身份/reauth 截止和结果。validation 用 request_record_id 反查请求，不与 HTTP request_id 混用。网络测试在事务外进行；工作进程重启后租约过期先核对结果/现有 release/request 唯一键，再恢复本地准备步骤。已经失去确定结果的网络探测标 unverified，不无限重跑。发布事务一次性写 release、head、引用索引、审计、Outbox 和幂等结果。

同键同体进行中返回相同 202/状态地址；已提交但响应丢失时重放相同结果。7 天重放窗口内仍检查现行读取权限；超过窗口返回 40929/410，保留键摘要与已发布 result_ref，不能无声重新执行。请求执行 deadline/认证期限与重放窗口分开；7 天不是后台任务的最长执行时间。

## C3. 字段 schema、PATCH 和绑定

字段级 JSON Schema 的命名入口如下；禁止未声明的字段，API Key 只在写入操作中接受。

| DTO / Schema | 字段与语义 |
| --- | --- |
| DraftCreate | base_release_id（可空）、expected_head_version（无发布为 0） |
| GrantUpdateInput / GrantDeleteInput | config_role_key + expected_policy_version / expected_policy_version；均拒绝额外字段与客户端 controlled_cli |
| DraftTestInput | role_key、expected_draft_version、可选 budget=standard/local_cold；If-Match 与 Idempotency-Key；每次最多 3 请求、每次最多 96 输出 Token，local_cold 仅限可信私网 |
| DraftPayload | schema_version、role_bindings_raw、index_bindings。v2 完整 17 角色，首次修改 rag_query_expand 的 PATCH 将草稿升至 v3（18 角色）。ReleaseManifestRead 可读 v1/v2/v3。 |
| DraftPatch | operations 数组 1—64，按顺序、同一数据库事务应用 |
| ProfileInput | display_name、model_kind、adapter_type、api_protocol、vendor、base_url、model、model_revision、auth_mode、endpoint_policy_revision_id、context_window、tokenizer_id/revision、token_counting_policy、max_output_tokens、connect_timeout_ms、request_timeout_ms、embedding_dimension、parameters |
| PromptInput | system_text、template_text；变量/输出契约由角色注册表继承并校验，固定安全策略不可写 |
| RoleBinding | enabled、selection、profile_revision_id、credential_revision_id、prompt_revision_id、source_role、fallback、failure_mode |
| MemorySaveInput / MemorySettingsInput / MemoryDocument | 个人记忆全文保存/部分设置/当前文档；正文 UTF-8 字节数另由服务端校验，见 F6 |
| MemoryExtractionResult / MemoryClearInput / MemoryEmptyInput / MemorySnapshot | 提取结果及来源证据、清空、空命令、固定快照；流程以 F 为准 |
| ReleaseManifest | role_bindings_raw、resolved_static_bindings、follow_execution_roles、index_bindings 和 schema_version |
| RoleCredentialState | role_key、has_api_key、masked_suffix、credential_revision_id、credential_version、status、auth_mode |

PATCH 不做隐式深合并。每条 operation 替换指定的完整对象，省略对象不改动；必填字段不能省略，显式 null 只在 schema 允许时清除。任何一项失败整体回滚；If-Match 不符不创建孤立版本。

字段 Schema 不代替服务器跨字段、权限和能力检查。parameters 不能覆盖凭据、地址、客户端、协议开关或重试控制；端点 connection_rules 用服务器白名单解析，拒绝 URL userinfo 和明文密码/Token 等敏感子字段，不能借 JSON 参数绕过 SecretStore。

- replace_profile：role_key、完整 ProfileInput、credential 动作；新建候选 profile，绑定改 explicit，保留未指定的业务模板/降级配置。Embedding 需显式 index_keys，按声明用途更新同空间查询连接；不兼容只保留待重建差异，不发布。
- replace_prompt：role_key、完整 PromptInput，新建模板修订，保留模型选择。
- replace_binding：完整 RoleBinding 替换角色绑定，不改变被引用版本内容。
- replace_index_binding：index_key、完整 IndexBinding；M1 只能引用原空间和现有索引版本，不能通过此操作绕过迁移。

credential 是 keep/replace/clear 的带标签对象；replace 需要 api_key，其他动作禁止 api_key。改 origin 不准 keep，需新凭据或无认证；明文仅在本事务加密为候选后丢弃，不写 draft.payload/action_payload。DraftView 返回 payload、profiles/prompts 的脱敏版本视图、credential_states 数组、反向引用、验证状态、id/etag/draft_version/base_release_id/base_head_version/status/expires_at。

credential_states 按角色标识，一角色一行，不能把一个对象误当全部 Key；follow_role 显示解析后的凭据；follow_execution 显示 status=dynamic、has_api_key=null 和空版本，不伪造某条固定凭据；未认证/未配置/禁用为独立派生状态。index_credential_states 另按 index_key 返回实际查询凭据。任何对象都不返回 ciphertext/api_key/proof。

failure_mode 封闭枚举为 error、rule_only、react_fallback、disabled、default_title；按 A 的角色限制校验。follow_role 禁止环、越权及能力不兼容。原始绑定持久存 role_bindings_raw，follow_role 的编译结果存 resolved_static_bindings；follow_execution_roles 只列动态 synthesize，不能声称发布时已选出其实际模型。请求内解析结果记录到 trace 的实际 profile/credential/model，并固定父快照。

GET /drafts/{id}/resolution 返回相同编译视图、reverse_refs、affected_roles、embedding_diff、validation_coverage；它不修改草稿、不调用模型。release 的 hash 包含原始/静态/动态三部分，任何引用变化都重新验证一致性。

验证 target_spec 包含：role_key/角色契约版本、所有影响请求的规范化模型参数、credential_revision_id/version、adapter_type/version、endpoint_policy_revision_id、safety_policy_version、probe_suite_version、canonicalization_version。target_hash=SHA-256(JCS(target_spec))，hash_algorithm=jcs-sha256-v1；按 [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) 规范化，不把 Python 字典输出当通用 JCS。明文密钥不入 target_spec；HMAC 请求哈希与该公开配置哈希用途不同。结果保留 target_spec、tested_at/valid_until 及失效原因，不用不透明哈希掩盖缺字段。

## C3.1 Rebase、导入、计数与历史恢复

Rebase 用 base_release、当前 head 和草稿做三方合并，以每个角色绑定/索引绑定为原子单元。两边改同一单元且不同则返回 40925，data.conflicts 列 JSON Pointer；不改原草稿。客户端可提供 resolutions(path, choice=ours|theirs)，显示差异后显式选择。无冲突或已解决后，用 If-Match 与 expected_head_version 再核对，更新基线、draft_version/ETag、失效验证、引用索引，回到 editing。最新 head 又变了返回 40920，不丢草稿。

一次性 CLI 导入也必须生成合法关系：受控 actor（现有 active 用户/配置管理员）、request(action=bootstrap.import、确定性 request_key)、draft、候选凭据/模型/模板及 draft_items、验证、release/release_refs、head、审计、Outbox。CLI 不伪造 HTTP Token，授权来源仅内部调用入口 controlled_cli，客户端不能提交同名参数绕过 reauth。request_key 绑定 importer_schema_version 和作用域；源内容 hash 不同返回 40922，已有发布时禁止“重新 bootstrap 覆盖”，只能创建新草稿。

导入先创建 head(NULL,0,0) 与 draft，再创建带来源 draft_id 的候选版本；读取 env、提示词和 config/agent.yaml 中 critic_model、token_budget。缺少可信上下文数据标为兼容基线而非模型能力事实。初始草稿/请求外键不放宽为 NULL。

profile/prompt/credential/index/endpoint_policy/price 修订用 counters.last_num 行锁分配；release_number 仅从锁定 head.last_release_number+1 分配，不存在第二个计数来源。head 首行 INSERT ... ON DUPLICATE KEY 后 SELECT FOR UPDATE；发布先写 release，再单条 UPDATE 同时改 active_release_id、head_version+1、last_release_number。affected_rows=0 整体回滚，禁止留下孤立 release。固定锁顺序为授权 head -> draft -> config head -> 引用控制行；多计数器按 (entity_kind,entity_key) 排序加锁。

restore 创建新 draft/request/release，保留 restore_from_release_id。只恢复曾发布的同作用域清单时允许 historical_offline 模式复用原能力证据（即使 24 小时已过），仍强制离线校验凭据未撤销、endpoint/安全策略有效、索引空间/水位与当前兼容、引用未清理、权限/reauth 有效。不是重新启用未知新配置，故不依赖故障供应商在线；UI 标示历史验证时间和未重新联机，不承诺模型此刻可用。有任何新模型/凭据/origin/能力组合则按正常验证；不允许借 restore 混入未验证字段。

## C3.2 计数与 head CAS 的执行模板

以下是带 SQLAlchemy 命名参数的事务模板，不直接交给 mysql CLI；完整发布字段仍以 B/E 为准。release 分配只使用 head；counter 不允许 entity_kind=release。

```sql
INSERT INTO ai_version_counters
  (scope_type,scope_id,entity_kind,entity_key,last_num,updated_at)
VALUES (:st,:sid,:kind,:entity_key,0,UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE entity_key=entity_key;
UPDATE ai_version_counters
   SET last_num=last_num+1,updated_at=UTC_TIMESTAMP(6)
 WHERE scope_type=:st AND scope_id=:sid
   AND entity_kind=:kind AND entity_key=:entity_key;
SELECT last_num FROM ai_version_counters
 WHERE scope_type=:st AND scope_id=:sid
   AND entity_kind=:kind AND entity_key=:entity_key FOR UPDATE;
```

同事务创建/锁定 head，先核对授权、draft_version 和 expected_head_version，再以锁内 last_release_number+1 写入新 release 与引用；最后执行：

```sql
INSERT INTO ai_config_heads
  (scope_type,scope_id,active_release_id,head_version,last_release_number,updated_at)
VALUES (:st,:sid,NULL,0,0,UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE scope_id=scope_id;
SELECT head_version,last_release_number,manager_generation
  FROM ai_config_heads WHERE scope_type=:st AND scope_id=:sid FOR UPDATE;
UPDATE ai_config_heads
   SET active_release_id=:release_id,head_version=head_version+1,
       last_release_number=:next_release_number,updated_at=UTC_TIMESTAMP(6)
 WHERE scope_type=:st AND scope_id=:sid AND head_version=:expected_head_version
   AND manager_owner_id=:owner AND manager_generation=:generation
   AND manager_lease_expires_at>UTC_TIMESTAMP(6);
```

UPDATE 影响行数必须为 1；0 行区分 head 冲突或管理租约失效并整体回滚。不能提交之前已插入的 release。初始化也使用同一完整发布事务；启动取得管理租约后才允许提交，不给 CLI 人造一个绕过 lease 的 UPDATE。

## C4. 状态字典与生命周期

| 对象 / 字段 | 允许值 | 备注 |
| --- | --- | --- |
| drafts.status | editing/validating/ready/published/abandoned/expired | 编辑增加 draft_version，旧验证失效；7 天过期 |
| validations.status | pending/running/passed/failed/unverified/cancelled | supported/unsupported/unverified 能力结论在 result，非同一状态字段 |
| requests.status | pending/running/succeeded/failed/expired | expired 指执行时限结束，replay_until 另管重放 |
| outbox.status | pending/inflight/succeeded/retry/dead_letter | 5 次有界退避后告警，不能把失败通知当未发布 |
| grants.status | active/revoked | config_role_key 与业务 role_key 分开 |
| endpoint_policies.status | active/revoked | 内容不可变，撤销只改控制状态 |
| secrets.status | candidate/active/retired/revoked | candidate 只能用于所属草稿测试 |
| profiles/prompts.status | candidate/published/retired | 不修改版本内容，受引用保留约束 |
| rag_index_versions.status | baseline_verified/baseline_unverified/ready/retired/building/catching_up/failed | M1 只用前四种，后三种 M3 |
| usage_status / pricing_status | known/unknown/missing；priced/unknown/legacy_estimate | NULL 金额和零单价不同 |
| credentials DTO status | candidate/active/retired/revoked/not_configured/dynamic/disabled | 后三种为读视图派生状态 |
| draft.embedding_state | unchanged/connection_updated/pending_rebuild | pending_rebuild 不是索引表状态 |
| migration_jobs.status（M3） | queued/building/catching_up/validating/ready_to_switch/switched/failed/cancelled | M1 不创建 Job/百分比 |

验证失败回 editing 并记录原因；validating 草稿被改动时取消旧任务的发布资格。ready 必须满足当前 hash/能力/策略。published 草稿只读，abandoned/expired 不可编辑，可复制成新草稿。rebase 成功也回 editing。

发布事务登记完整 release_refs 并提升被引用候选；draft_items 记录草稿创建/引用对象。放弃/过期后 24 小时内将无发布/其他有效草稿/验证租约/在途引用的候选退役，7 天后按依赖顺序清理仅草稿版本与密文行，审计留 ID/脱敏事件；不能因多态 JSON 不可反查而永久留存。引用注册、退役和清理均事务化、失败可重试。

页面 dirty 是本地未保存；editing/validating/ready 映射草稿/验证中/已验证；head 指向该 release 才叫已生效。M1 pending_rebuild 显示“仅可保存，M3 迁移能力未启用”；M3 才显示真实 Job 进度。两种状态并列，不展示虚构百分比。

## C5. 凭据轮换与紧急撤销

| 位置 | 普通 replace 并发布 | revoke 提交后 |
| --- | --- | --- |
| 未解析快照 | 使用新 release/凭据 | 新出站许可拒绝 |
| 已解析但未取得许可 | 固定原凭据；发出前检查有效性 | DB 有效性检查拒绝，不改用新 Key |
| 已取得许可/已发送/流中 | 使用旧凭据到本轮结束；外部撤销导致明确错误 | 注册表尽力取消并记录；上游已受理效果不能撤回 |

授权线性化点是发送前数据库许可检查；DB 提交与 HTTP 不组成原子事务。撤销前已取得许可的竞态属于在途。M1 同进程取消和 Outbox，M2 多进程通知/状态巡检是后续增强，不能写成零延迟远端取消。返回 dispatch_blocked/cancel_requested，不返回未经证实的 remote_cancelled=true。普通 replace 不自动 revoke；控制存储不可用则不发新调用。

已实现的撤销事务原子写入凭据 status=revoked、递增 revocation_epoch、撤销时间、脱敏审计和 credential.revoked Outbox；不移动活动发布 head。首次写入需现行 config.credential.revoke 权限和绑定当前 access_jti 的重新认证。相同幂等键/输入只重放已提交回执，异输入返回 40922；已撤销版本用新键再次确认也不递增 epoch。新动作的 reason 必须为非空文本，最长 512；页面只提供固定原因类别。自由文本只留 HMAC 指纹，不写审计/请求正文。回执包含 credential_revision_id、credential_version、status、revocation_epoch、dispatch_blocked、cancel_requested、already_revoked，不包含密文或真实 Key。

运行时与探测在 MySQL 使用凭据当前共享读，避免旧事务的一致性快照放行。HTTP 租约先于发送许可登记，并覆盖响应头和响应体读取；提交后立即请求取消同进程相关 HTTP 工作，Outbox 负责重试。运行中探测变为终态 cancelled，迟到结果不能覆盖；历史 passed 证据保留，恢复仍检查撤销状态。无认证 Ollama 没有凭据版本，不伪造撤销对象。

影响范围针对所有引用该版本的配置。其他版本即使保存相同 Key，也不自动撤销；供应商侧实际 Key 需另行停用。影响读取不是全局冻结快照，确认期间可能新增引用；撤销仍覆盖目标版本全部引用。草稿数量包含历史登记引用，动态角色只列潜在影响。

活动配置引用已撤销凭据时，允许创建和逐项编辑修复草稿，返回 CREDENTIAL_REVOKED 等 publication_blockers；这不授予运行或发布资格。保留原凭据的模型重存、测试和发布仍拒绝，需替换新版本并重新验证。候选草稿归属和作用域检查保持生效。

## C6. 重试、原生向量与成本

ClientRegistry 明确 ChatOpenAI/OpenAI max_retries=0、httpx.AsyncHTTPTransport(retries=0)；不能保留 factory 原来的 retries=1。原生 DashScope 适配按调用关闭其内部重试；若复用 LangChain tenacity 封装，max_retries 在该实现表示尝试次数，应设 1 并用调用计数测试，而非照抄 OpenAI 的 0。

适配器在无正文/工具执行时，对连接超时、429、临时 5xx 最多再试 1 次，受 Retry-After、deadline、Token/费用约束；400/401/能力错误不重试。已输出正文不重跑整流，已执行工具不重跑整个 Agent，未知使用量不记零。降级仅在本 release 已授权角色链中进行。

本地实现已覆盖发布配置中的 OpenAI 兼容聊天与 Ollama 原生聊天。连接超时或 HTTP 429、500、502、503、504 可在读取响应体前对同一端点追加一次尝试。发送沿用原请求体和凭据版本，重新走当前权限、端点和凭据校验。两次发送与等待响应头共用配置请求时限，外层任务超时和取消仍能中断。等待上限为两秒，支持 Retry-After 的秒数和 HTTP 日期。收到成功响应头后进入单次响应读取。尝试数和单次输出上限参与控制，精确费用预算仍待验收。[聊天模型重试记录](../实施记录/M1_聊天模型有限重试_2026-11-18.md)

发布配置请求可在 `task_model_budget` 启用共享调用次数、Token 与原币种费用限制。`42903` 的 JSON `data.reason` 和聊天 SSE `reason` 可能为 `MODEL_CALL_LIMIT_EXCEEDED`、`MODEL_TOKEN_LIMIT_EXCEEDED`、`MODEL_COST_LIMIT_EXCEEDED`、`MODEL_CONTEXT_BOUND_UNKNOWN`、`MODEL_PRICE_UNAVAILABLE`、`MODEL_PRICE_LOOKUP_FAILED`、`MODEL_BUDGET_CURRENCY_UNAVAILABLE` 或 `MODEL_USAGE_EXCEEDS_DECLARED_BOUND`。聊天 SSE 事件含 `type=error`、`code=42903`、`reason`、`retryable=false` 和 `content`，结束后无 `done`。[预算实施记录](../实施记录/M1_请求内模型预算_2026-11-18.md)

备用模型启用后，第二次尝试使用同一快照中的已验证备用配置。每个角色支持一个固定的聊天备用角色。主备须使用同一协议，备用模型的声明窗口至少等于主模型，单次输出上限和计数配置一致。同服务沿用同认证身份校验。跨服务时须使用两个独立端点策略，策略的环境和网络类型相同，且 `connection_rules.allow_cross_service_fallback` 均为 `true`。出站时重新核验主备权限，备用请求使用备用配置自身的认证信息。[实施记录](../实施记录/M1_显式Responses与跨服务备用_2026-11-19.md)

能力测试输入可选 `fallback_role_key`。有该字段时按 `role_key` 的能力契约测试所绑定备用模型，目标哈希包含双方配置与凭据版本关系。同一草稿版本内主模型与备用模型分别验收。已有历史发布使用保存的验证引用恢复并核对当前凭据。第二次发送前重新检查主备的授权与撤销状态。授权拒绝、收到成功响应后的解析或流错误均直接结束本次调用。

M1 增 dashscope_native/dashscope_embed，保留现有原生协议、model、dimension/text_type 等参数和空间身份；切到兼容向量端点是 M3 候选，不能仅凭少量探测免重建。当前 DashScopeEmbeddings 在验证时设置全局 dashscope.api_key，实际 call 未显式传 Key；不能照搬此全局状态。新原生包装器每次显式 api_key/base_address，保留 document/query 语义，遵守同一出口/超时策略并计数重试；本机锁定 SDK 支持这些参数。不能安全注入受控传输时保留固定原生地址并禁用自定义 URL，不关闭地址守卫。M1 验证在途旧 Key 与新 Key 无串用。

/api/v1/usage/summary 同步迁移：data.currency_totals=[{currency,priced_amount,unknown_usage_count,unknown_price_count,legacy_estimate_count}]、cost_status=complete|partial|unknown。未知金额为 null 并告警，不继续将未配置模型按 0；不直接相加 USD/CNY。旧 cost_cny 字段保留兼容，仅在真实 CNY 或有记录的汇率版本时返回数值，否则为 null，前端 Usage/API 类型一并适配。历史费用无可信报价只标 legacy_estimate/unknown，不回填假价格。

此接口已返回按币种聚合的 `currency_totals`。`priced_amount` 和 `legacy_estimated_amount` 使用十进制字符串或 `null`。调用未收到完整 Token 用量时计入 `unknown_usage_count`。价格版本缺失时计入 `unknown_price_count`，对应币种可为 `null`。`unknown_cost_count` 按调用去重。`total_cost_cny` 和模型维度 `cost_cny` 仅反映采用版本价格的 CNY 小计。配置测试通过 `config_test_usage` 独立汇总。[实施记录](../实施记录/M1_版本化计价与多币种用量_2026-11-18.md)

已发布模型声明窗口时按最终发送内容进行检查。`MODEL_CONTEXT_LIMIT_EXCEEDED` 返回 `context_window` 与 `estimated_total_tokens`，聊天 SSE 事件同时包含 `type=error`、`code=40008`、`retryable=false`。该事件结束本轮流式处理。保存聊天模型时，声明窗口须大于有效输出上限，否则返回 `MODEL_OUTPUT_EXCEEDS_CONTEXT`。[实施记录](../实施记录/M1_模型上下文与Token计数_2026-11-18.md)

事务型笔记与回顾工具完成时，聊天 SSE `tool_end` 可携带 `operation_id` 和 `replayed`。执行凭据绑定已提交的用户消息，由服务端生成。聊天重复 `idempotency_key` 输出 `type=error`、`code=40902` 和 `content`，同轮工具结果复用通过独立的执行凭据完成。[实施记录](../实施记录/M2_事务型工具去重_2026-11-18.md)

聊天首个进度事件与 `done` 可携带 `workflow_id`。`GET /api/v1/chat/workflows/{workflow_id}` 返回所属会话的最新检查点，包含 `release_id`、`status`、`route`、`finished_steps`、`state_version`、`turn_message_id`、`reply_message_id`、时间戳与稳定 `error_code`。未完成记录租约过期时状态为 `interrupted`。非所有者或已清理记录返回 404。查询允许当前无活动模型发布。[实施记录](../实施记录/M2_对话执行检查点_2026-11-18.md)

可补读的实时 SSE 进度及成功 `done` 带 `sequence` 与 SSE `id`。`GET /api/v1/chat/workflows/{workflow_id}/events?after=0&limit=50` 返回所有者可见的 `events`、`cursor`、`has_more` 和 `status`。恢复用的 `done.replay_content` 来自同会话已提交的助手消息，前端将它作为完整回复替换当前片段。取消、失败或中断通过终态 `error` 表达。[实施记录](../实施记录/M2_SSE事件续读_2026-11-18.md)

`POST /api/v1/chat/query` 可选 `continue_on_disconnect=true`，在已发布模型模式下启动后台对话。后台对话默认只读，响应头返回 `X-Workflow-Id`。所有者可 `POST /api/v1/chat/workflows/{workflow_id}/cancel` 跨进程停止后台对话。成功提交取消的状态为 `cancelled`，`error_code=USER_CANCELLED`。结束后重复停止返回其终态，运行中的普通对话返回 HTTP 409 和 `WORKFLOW_NOT_DETACHED`。工作流详情以 `is_detached` 标识后台模式。[实施记录](../实施记录/M2_后台对话跨进程取消_2026-11-18.md)

后台对话返回的 `X-Workflow-Id` 对应已提交的 MySQL 任务。首次领取前 `status=running` 且 `execution_attempt=0`。领取后该序号从 1 开始递增。未领取的任务允许显式取消。SSE 若提前结束，客户端可按事件序号继续补读。`job_deadline_at` 表示受理后的总截止时间。[实施记录](../实施记录/M2_后台对话持久派发和Worker恢复_2026-11-19.md)

后台请求可显式设置 `allow_background_actions=true`，使笔记、回顾和邮件工具能够提出待确认操作，并开放 PPT 与语音生成。每个 Plan 步骤或单次 ReAct 对话绑定一个副作用回执。后台恢复复用原操作标识及状态。已确认落盘的文件通过带 `operation_id` 的 `tool_file` 事件回放，下载继续使用所属用户的鉴权入口。[实施记录](../实施记录/M2_副作用步骤恢复与阶段验收_2026-11-19.md)

Worker 接管符合条件的计划后，复用的步骤仍产生原有 `plan_step_end` 事件。工作流详情与补读接口沿用步骤号和事件序号，服务端检查点仅用于内部恢复。[实施记录](../实施记录/M2_只读计划步骤恢复_2026-11-19.md)

`GET /api/v1/chat/sessions/{session_id}/approvals` 读取会话所有者的待确认操作，返回 `operation_id`、`tool_name`、`arguments`、`target`、`expires_at` 和 `preview_hash`。`POST /api/v1/chat/approvals/{operation_id}` 只接收 `decision`（`approve` 或 `reject`）与 `preview_hash`。摘要不匹配返回 HTTP 409。决定提交后返回 `executed`、`rejected`、`expired` 或 `conflict` 及已有执行回执。重复请求保持对应终态。读取和决定响应均禁止缓存。[实施记录](../实施记录/M2_AI写入审批_2026-11-19.md)

`tool_name=send_email` 时，预览包含 `to`、`subject`、`body`、`format`。确认后以 `dispatching`、`accepted`、`failed`、`unknown` 或 `received` 反映邮件状态，并返回 Message-ID 等回执元数据。`GET /api/v1/chat/sessions/{session_id}/email-operations` 供所有者重读发送结果。`POST /api/v1/chat/approvals/{operation_id}/email-received` 记录所有者的收件核实，其状态是 `received`。重复确认只读取保存的发送状态。两个邮件记录接口均禁止缓存。[实施记录](../实施记录/M2_邮件审批与结果核对_2026-11-19.md)

## C7. 发布与响应枚举

所有 GET/PATCH 的敏感内容脱敏。成功发布返回 data.release_id/release_number/head_version/published_at、effective_for=new_requests、embedding_state=unchanged|connection_updated；202/未配置为 effective_for=none。M3 才能 index_switched。回滚是新发布，同样递增 head/release_number。

API 与 DTO 的 Schema 规则、状态字典和 DDL CHECK 同步维护；M0 错误信封/审计扩展、M1 数据迁移分别在附件 D 有明确证据和门禁，不把本文件本身当已实施证明。

## C8. 通知查询与人工补投

本批只管理 model_config.published / credential.revoked。GET 返回 id、event_type、aggregate_id/version、status、attempts、next_attempt_at、lease_expires_at、受控 error_code、created_at、redrive_event_id；不返回事件 payload、event_key、lease_owner 或底层异常。只支持同 system 作用域，不能通过 ID 读取其他作用域。

OutboxRedriveInput 包含 expected_attempts（0—5）和固定 reason=operator_retry，拒绝附加 payload、Key 或自由文本。新动作需 config.outbox.manage 和绑定当前令牌的重新认证。只允许源事件仍为 dead_letter、尝试次数匹配且未创建过补投事件；状态变化或已经补投返回 40923，reason 分别为 OUTBOX_STATE_CHANGED / OUTBOX_ALREADY_REDRIVEN。损坏的业务事件体返回 OUTBOX_EVENT_INVALID，不从客户端补写。

补投事务锁定源事件，创建新 pending 事件、脱敏审计与幂等回执；不修改源事件状态、次数或租约。每个源事件的 redrive:<source_id> 事件键唯一；并发同幂等键重放同一结果，异键也不能重复建子事件。补投事件再次失败进入死信后，可由管理员对该子事件另行确认补投，形成可查链；每一事件最多自动尝试 5 次，不重置旧记录的计数。相同键异输入返回 40922，事务失败不留子事件或成功回执。

OutboxRedriveResult 返回 source_event_id、event_id、status=pending、attempts=0、original_attempts、delivery_guarantee=at_least_once、config_head_changed=false。这是提交时排队回执，不代表此刻送达，重放也不把历史回执改成实时状态；需 GET 新事件查询结果。页面保留失败提交的幂等键，重试重新认证，不保存 proof 到浏览器存储。

Worker 在事务内取得授权 head、管理 head、事件的锁后领取，处理在事务外进行；每次处理最多 10 秒，事件租约 15 秒。完成时核对领取时的管理 owner/generation、现行有效管理租约，以及事件 owner/attempts/状态/有效租约；不能自行续租把旧工作当新一代完成。取消/崩溃留下的 inflight 事件在租约到期后可重领；最后一次租约到期进入死信并清除租约。迟到成功或失败均不能覆盖新尝试。可能重复投递，处理方必须幂等。

发布通知只核验当前已提交快照，撤销通知只重读撤销状态并请求同进程取消；均不重新发布历史 release、不恢复凭据、不调用模型。通知失败不回退已提交的配置。当前页面提供可查状态，自动外部告警和多进程分发未在本批实现。

## C9. 候选配置生命周期（2026-11-17 增量）

实现及证据见[生命周期记录](../实施记录/M1_候选配置生命周期与测试产物清理_2026-11-17.md)。AI_CONFIG_CANDIDATE_GC_ENABLED=true 显式启用后台扫描，默认关闭；AI_CONFIG_CANDIDATE_GC_PURGE=true 另行允许正式删除，默认只退役。生产删除必须提供 AI_CONFIG_MAINTENANCE_DATABASE_URL，校验目标主机/端口/库/数据库类型与业务库一致、账号与业务账号不同；不自动授予 DELETE 权限，最小权限仍由部署配置和验收。

任务沿用授权 head → 管理 head → 草稿/候选的事务锁顺序，一批最多 20 个草稿、每种候选每草稿最多 100 行，游标仅在提交后推进。常规每小时扫描，存在分页积压或失败时 60 秒继续。editing/validating/ready 超时后标记 expired；abandoned/expired/published 来源下未被使用的候选可退役，published/active 对象不作为候选删除目标。密文/内容原地不改写。

退役前和删除前检查 release_refs、实际历史发布清单、有效草稿的 inventory 与实际 payload、有效验证证据/租约/截止、索引 build_profile、提示词来源链和同进程凭据 HTTP 引用。删除凭据还要求派生 profile 已删除，待处理撤销事件也会阻止清理。仅退役满七天后可按 profile/prompt → credential 的顺序删除，同时清理相应 draft_items；草稿、版本计数器、请求、验证和脱敏审计保留，不复用版本号。撤销候选保持 revoked，不因退役取消撤销。

退役、删除与脱敏审计同事务，固定维护 actor=system:config-lifecycle，不伪装成某位用户。事务失败整体回滚，分页游标不前移；管理租约无效则停止本批。无历史发布记录的 retired 候选不能通过状态改变获得跨草稿使用或发布资格。保留期结束后，读取已放弃且引用已清理的草稿返回 HTTP 410 / DRAFT_REFERENCES_PURGED；过期草稿继续使用原过期错误。

终态死信记录保留，但其未发布凭据可在满足上述条件后清理。人工补投撤销通知时新增目标状态检查，凭据已清理/不再撤销则返回 40923 / OUTBOX_TARGET_UNAVAILABLE，不再创建必然失败的补投事件。部署开启维护任务需要重启加载；本轮未对运行中的业务服务或数据执行该操作。

## C10. 主密钥文件轮换准备与恢复预检（2026-11-18）

新增受控本地命令 prepare-keyring-rotation（source/output/new-version），只生成独立的新文件，保留全部旧版本，active_version 指向新生成的 Fernet 密钥；不覆盖源/目标，不修改 .env，不更改密文行或发布指针。激活需要部署时显式切换 AI_CONFIG_KEYRING_FILE 并重启单进程服务。历史密文按原 key_version 解密，后续新建/替换的凭据使用新 active_version。

新增 inspect-keyring（path/user-id/check-decryption），由当前 active config_admin 使用本地进程权限调用，复用 config.publish 授权检查；不开放新的 HTTP 绕过入口。只读事务返回密文按版本/状态的计数、仍在重放窗口内的配置与个人记忆请求版本、缺失版本；--check-decryption 验证密文的 scope/id/origin/version 身份绑定，失败明细最多 100 条、另有总数和截断标志。无密钥正文、明文凭据、HMAC 值或记忆内容。检查失败返回退出码 2，文件/授权/存储操作错误返回 1。

未请求解密检查时结果为 null，不把版本名存在当成正确密钥证明；错误材料使用同一版本名也会在解密检查中失败。个人记忆表不存在时显式报告缺表，不声称已覆盖其引用。盘点是只读观察，不是备份、热切换、旧密钥删除许可或密码学材料的自动销毁依据。

发布和历史恢复在提交 release/head 前验证全部引用凭据的解密，缺少/错误密钥返回 50303 并回滚，不留下半发布。新 active_version 生效后旧重新认证 proof 失效；保留旧材料支持原版本 HMAC 重放及旧快照解密。旧密文不自动复制成新凭据版本，普通替换仍走新版本、能力测试和发布流程，撤销规则保持不变。

密钥读取拒绝重复 JSON 字段/版本、未知根字段、无效类型/编码/密钥、超 1 MiB 文件或超 128 个版本，错误不带原始解析上下文；缺文件绝不自动重建。文件准备使用同目录临时文件、flush/fsync 和不可覆盖的硬链接安装；不支持该方式的文件系统明确失败。文件目录权限、Windows ACL 和灾难断电恢复另需部署验收，不能把本地调用成功当成已证明。

隔离恢复验证使用包含撤销状态的数据库备份；恢复到更早时间点时，之后发生的撤销必须另行补齐，不能声称自动恢复备份中不存在的状态。实施范围、步骤、证据及限制见[主密钥实施记录](../实施记录/M1_主密钥轮换准备与隔离恢复验证_2026-11-18.md)。
