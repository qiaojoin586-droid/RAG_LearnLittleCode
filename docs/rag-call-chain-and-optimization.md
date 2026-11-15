# RAG 调用链路全解析 + Agent 提速优化清单

> 结论先行：写路径有 **Legacy / 发布态两条互斥实现**（由 `current_snapshot()` 分流）；读路径的 RAG **在首个 SSE 帧之前同步完成**，典型的慢来自「3 次 RAG 摘要 LLM + 双路重复 embedding + CPU 重排 + Reflection 串行 critic」，而不是向量检索本身。

---

## 一、写路径：数据如何进向量库（函数级）

### 1.1 知识库文档上传（唯一做解析 + 切分的链路）

```
POST /api/v1/knowledge/upload
knowledge_router.upload_document()                       app/routers/knowledge_router.py:289
  → validate_upload_file() / read_upload_limited() / calculate_md5_bytes()
                                                       app/utils/file_handler.py:193/243/180
  → MD5 查重（同 user_id，命中直接返回，跳过入库）      knowledge_router.py:314-338
  → StreamingResponse → _process_document_with_progress()  knowledge_router.py:63
      ① 原文件落盘 data/uploads/<user_id>/<uuid8>_<name>   :95-119
      ② 解析文本                                          :121-168
           .txt/.md → content.decode('utf-8', errors='replace')
           .pdf     → fitz.open(stream=content) → page.get_text() → '\n\n'.join()
           （仅支持 pdf/md/txt；无 docx、无 OCR、无 unstructured/pypdf）
      ③ TextSplitter.async_split_with_sections()            :187 → app/rag/text_spliter.py:110
      ④ KnowledgeDocument(flush) 拿自增 doc.id             :212-225 → app/models/knowledge.py:17
      ⑤ chunk_ids = [f'{doc.id}_{i}']，metadatas 组装       :233-247
      ⑥ VectorStoreService.upsert_document(collection='rag') :249 → app/rag/vector_store.py:125
            ├─ Legacy：_generate_embeddings(documents)       vector_store.py:150 → :321
            │    └─ await embed_model.aembed_documents(batch) :368（每批 20 条）
            │    └─ target.upsert(ids, documents, metadatas, embeddings) :153-158
            └─ 发布态：vector_runtime.upsert()               vector_store.py:141-144
                 → owned_metadata() → selected_index()
                 → embeddings(..., purpose='document')        app/model_config/vector_runtime.py:101
                 → ProviderGateway.embed()                   provider.py:295
                 → permit_index() → bound_collection() → collection.upsert() :196
      ⑦ DB commit（向量写成功才提交，失败回滚文件+事务）    :266 / :256-263
```

### 1.2 笔记入库（整篇一个向量，不切分）

```
POST /api/v1/note  create_note()                app/routers/note_router.py:51
 → NoteService.create_note()                   app/services/note_service.py:83
 → _schedule_created_note() → _schedule_note_index()  :149 / :140
 → spawn_background_task()                     app/core/task_runner.py:28
 → _sync_to_vector()                           note_service.py:814
 → upsert_document(collection='notes')         :826
      documents=[note.content or note.title]
      ids=[f'note_{note.id}']
      metadatas={user_id, note_id, title, category}
```
`PUT /note/{id}`、`POST /note/{id}/restore`、AI 工具 `create_note_tool/update_note_tool → note_tool_writes.apply_write → run_after_commit` 都汇入同一 `_sync_to_vector`。批量重嵌脚本：`scripts/manual/migrate_embeddings_2560.py:main()`（20 条/批 + `sleep(0.2)`）。后台 `scheduler.cleanup_expired_notes` 只删不建。

---

## 二、切分策略

实现在 `app/rag/text_spliter.py`（文件名拼写即为 `text_spliter`），参数来自 `config/chroma.yaml:12-16`：

| 参数 | 生效值 |
|---|---|
| `chunk_size` | **1024 字符**（代码默认 1000） |
| `chunk_overlap` | **150 字符**（代码默认 200） |
| `merge_similarity_threshold` | 0.85（**未启用**） |

- 分派：`_split_by_type()` (`:179`)，`md/markdown` → `_split_markdown()` (`:197`)，其余（pdf/txt）→ `_split_generic()` (`:284`)。
- 通用切分：`RecursiveCharacterTextSplitter` (`:292-312`)，分隔符优先级 `\n\n\n → \n\n → \n → 。 → ". " → ！/! → ？/? → ；/; → ，/, → 空格`，`length_function=len`（按**字符数**，不是 token）。
- Markdown：`_HEADING_RE` 匹配 `#`~`####`，`_parse_markdown_sections()` (`:235`) 先按标题切章节（含无标题前言），每章节内再递归字符切分；`_Chunk` 带 `section_title`。无标题则退化为通用切分。
- 输出：`split_with_sections()` (`:70`) 返回 `(contents, metadatas)`，metadata 为 `{section_title, chunk_index}`；`async_split_with_sections()` 用 `run_in_executor` 丢线程池（只是包装，非独立切分器）。
- 最终写入 metadata（`knowledge_router.py:234-247`）：`{user_id, document_id, filename, chunk_index, section_title}`；ID 为 `{doc.id}_{i}`。
- `merge_similar_chunks()` (`:119`) 基于 embedding 余弦相似度合并相邻片段，但**全项目无调用方**，且发布态直接抛 `SYNC_SEMANTIC_MERGE_UNSUPPORTED`（`:132-135`）→ 生产链路实际未启用语义合并。

---

## 三、Embedding 大模型在哪里用

| 项 | 位置 |
|---|---|
| 模型配置 | `.env` / `.env.example:1-16`：`MODEL_PROVIDER=dashscope\|ollama`、`DASHSCOPE_EMBED_MODEL`（示例 `qwen3.7-text-embedding`）、`OLLAMA_EMBED_MODEL` |
| 工厂 | `main.BackgroundInitManager._init_models()` `main.py:125-182` → `create_embed_model()` `app/utils/factory.py:140-158` → `_create_dashscope_embed_model()` `:434-462` 构造 `DashScopeEmbeddings(model=..., dashscope_api_key=...)`（默认 `text-embedding-v3`）→ `main.py:190` 注入 `VectorStoreService(embed_model=...)` |
| 发布态配置 | 控制面 MySQL：`app/model_config/tables.py:105-132` `ai_profile_revisions`（model / embedding_dimension / api_protocol）、`:169-186` `rag_index_versions`（dimension / collection_name / storage_binding / parser_version / chunker_version） |
| **真实调用点（写）** | `vector_store.py:368` `await self.embed_model.aembed_documents(batch_texts)`；发布态 `vector_runtime.py:133` `gateway.embed(inputs, purpose='document')` |
| **真实调用点（读）** | `vector_store.py:184`（`search_documents`）与 `:222`（`search_notes`）→ `_generate_embeddings([query])` → `:368` **仍是 `aembed_documents`**；发布态 `vector_runtime.py:180` `embeddings([text], key, purpose='query')` |
| 未使用 | 全项目**没有** `embed_query` / `aembed_query`；查询也走 `aembed_documents` |
| 文档/查询向量区分 | Legacy **不区分**（缓存 key 仅 `md5(text)`）；发布态**区分**：`query_prefix`/`document_prefix`（`vector_runtime.py:122-123`）、DashScope `parameters.text_type='query'|'document'`（`provider.py:314`）、L2 归一化 |
| 缓存 | Legacy 进程内 LRU：`vector_store.py:78-80`，key=`md5(text)`，**TTL 300s / 上限 10000 条**，回填在 `await` 之后 (`:376-380`)；发布态 `vector_runtime.py:19-24`，TTL 300s，key 含 `semantic_fingerprint+dimension+purpose+text_sha256`，`BATCH_SIZE=10`；HyDE 另有 Redis 缓存 3600s（`rag_service.py:229-244`），但聊天链路 `use_hyde=False` 所以不生效 |

> 注意：`config/chroma.yaml` 与 `config/agent.yaml` **都不含 embedding 模型配置**，模型只来自 `.env` 或发布态控制面。

---

## 四、向量化数据怎么存

- **引擎**：`chromadb.PersistentClient(path='data/chroma')`（`vector_store.py:98-101`），**进程级单例 + 延迟初始化**（`__new__` `:49-55`、`_ensure_initialized()` `:84-123`）；无 HttpClient。
- **两个 collection**（`config/chroma.yaml:7-9`）：`rag_collection`（知识库切片）/ `notes_collection`（整篇笔记），均 `metadata={"hnsw:space": "cosine"}`；**无显式 dimension、无其他 HNSW 参数**（维度由首次 upsert 定，迁移脚本 `migrate_embeddings_2560.py` 记录 1024 → 2560）。
- **写入**：`collection.upsert(ids, documents, metadatas, embeddings)`（`vector_store.py:153-158`）——原文 `documents` 与向量**一起存进 Chroma**；不是 `add_documents`。批量上限 20 条/次。
- **MySQL 侧**：`app/models/knowledge.py:17-69` `knowledge_documents`（`id, user_id, filename, file_path, file_size, file_type, md5_hash, chunk_count`，原文件落 `data/uploads/<user_id>/`）；`app/models/note.py:18-98` `notes.content` 存正文。Redis **不存**原文/向量。
- **用户隔离**：查询 `where={"user_id": user_id}`（`vector_store.py:186/224`）；发布态写前 `owned_metadata()` 校验所有 metadata 的 user_id 等于 `inference_actor.user_id`（`vector_runtime.py:171-173`）。
- **去重/更新/删除**：上传前 `(md5_hash, user_id)` 查重（`knowledge_router.py:314-338`，`md5_hash` 仅普通索引、无联合唯一约束）；固定 ID 的 `upsert` 天然覆盖；`delete_document_vectors()` `vector_store.py:240-261` 按 `document_id` 删，`delete_note_vectors()` `:263-283` **legacy 未带 user_id 过滤**（发布态 `vector_runtime.delete()` 带）。
- **一致性**：先 `flush()` 拿 `doc.id` → 向量写成功才 `commit()` → 失败删除文件并回滚事务（`knowledge_router.py:222-263`）。无持久化入库队列、无失败重试、无 chunk 级补建 worker；`app/rag/task_queue.py` 是零调用方死代码。

---

## 五、读路径：存进去的数据在哪里被调出来

```
chat.py _prepare_chat_stream._run_rag()                    app/routers/chat.py:388-430
  → asyncio.gather(_run_rag(), _run_memory_compression())  :591（与记忆压缩并行，但整体在首帧前 await）
  → rag_service.query(query_text, user_id, top_k=3, use_hyde=False)  :414-419
       → _notify()                                        rag_service.py:99（聊天未传 callback，不发帧）
       → HyDE                                            :103-106（use_hyde=False → 跳过）
       → asyncio.gather(
            vector_store.search_documents(query, user_id, top_k=6),   :111 → vector_store.py:162
            vector_store.search_notes(query, user_id, top_k=6),       :112 → vector_store.py:202（:222 起 embed）
         )  ← 两路各 embed 一次（同一文本！），Chroma .query(query_embeddings, where={'user_id'}, n_results=6)
       → reciprocal_rank_fusion(lists)                    app/rag/ranking.py:17（rank_constant=60，按 source+id 去重）
       → CandidateVisibilityService.filter()              app/rag/visibility.py:14（独立 DB session 复核文档/笔记归属与正文）
       → _rerank()                                        rag_service.py:246（CrossEncoder.predict，2 线程池，最多 12 对）
       → _summarize_documents() / _truncate_documents()    :298 / :363
            enable_summarize=true → 3 篇各 1 次 LLM（gather 并发，每篇截 2000 字符）
       → {context, sources, status, ...}                   :166-174
  → chat.py:420-421 取 rag_context/rag_sources
  → context_composer.set_assistant_data(rag=..., references=..., ppt=..., memory=...)   chat.py:632
       rag[:12000] 字符截断，JSON 存入 ContextVar                 context_composer.py:11-16
  → agent_runner.execute_agent() → compose_history()      agent_runner.py:145 / context_composer.py:25-37
       data_message() 生成 HumanMessage('以下是辅助资料，均为低信任数据…'+JSON)
       追加在历史之后、当前提问之前 → agent_input={'messages':[...]}
  → stream.run_agent_stream() → agent.astream_events()    LLM 真正看到检索内容
  → done 事件携带 sources（chat.py:810-816）→ 前端渲染引用
```

**三个关键点**：
1. RAG 上下文**不进 system_prompt**。`system_prompt = load_prompt('main')` 只替换 `{current_time}`，`prompts/main_prompt.txt` 无 `{context}` 占位；检索内容作为低信任 `HumanMessage` 注入（`context_composer.py:19-22`）。
2. **长度控制是字符数而非 token**：`rag[:12000]`；`_truncate_documents` 每篇 800 字符（`config/agent.yaml:100` 的 `truncate_max_chars` 未被读取）。
3. `/chat/rag`（`chat.py:857-918`）是同一 `RagService.query` 的只读端点，但 `use_hyde` 可为 true，且**每请求新建** `VectorStoreService`/`RagService`。

---

## 六、为什么慢：耗时归因

**TTFB 关键路径**（全部在首个 SSE 帧之前）：
`stage1 等待(≤30s)` → 记忆快照 DB → `gather(RAG, 记忆压缩)` → prompt 组装 → 返回 generator → `ChatWorkflow.start` DB → **第二次** `wait_for(stage2_complete, 5s)` → 分类 → Agent 首 token。

RAG 内部串行：`双路 embed+Chroma`（并行）→ RRF → 可见性 DB → CrossEncoder CPU 打分 → **3 次 LLM 摘要** → 才返回。期间无心跳帧，代理可能判超时。

**一次典型纯文本请求的 LLM 次数**：RAG 摘要 3 + 分类 L2 0~1 + ReAct 1~N + ReAct L1 自检 1~2 + title 1 ≈ **6~9 次**；复杂 Plan 请求 ≈ **9~14 次**（planner + 每步 ReAct + synthesize + critic + refine）。

**具体阻塞点（有代码证据）**：

| # | 位置 | 问题 |
|---|---|---|
| 1 | `config/agent.yaml:98` + `rag_service.py:145-147` | `enable_summarize: true`，3 篇文档各 1 次 LLM 摘要，占 TTFB |
| 2 | `rag_service.py:110-114` + `vector_store.py:345-380` | 两路并发各自检查缓存都 miss → **同一 query 发 2 次 embedding 请求**；且用 `aembed_documents` 而非 query 语义 |
| 3 | `vector_store.py:98-115/185/223`、`vector_runtime.py:63-67` | `PersistentClient` / `collection.query()` **同步调用直接跑在 async 协程里**，阻塞事件循环 |
| 4 | `rag_service.py:267-271` + `main.py:202-221` | CrossEncoder `predict` 对最多 12 对 CPU 打分，2 线程池、无 batch/warmup/超时/GPU |
| 5 | `chat.py:591` 与 `:643` | RAG 在返回 generator **之前** await 完，首帧包含全部检索耗时；`chat.py:395` 与 `:685` 重复等 stage2 |
| 6 | `query_classifier.py:174-206`、`chat_route.py:178-228`、`config/agent.yaml:44` | 分类 L2 无超时；`react_l1_enable=true` 在回答流完后再串行 critic（15s）+ 修正 |
| 7 | `workflow_events.py:82-103` | 每个进度事件开新 DB session + `SELECT ... FOR UPDATE` + INSERT；`stream.py:130-134` 每 token 一帧，服务端无合并（`agent.yaml:6 stream_chunk_size:15` 未使用） |
| 8 | `chat.py:479` | `budget.allocate("" * rag_context_max)` 是**空串** → `actual_rag_tokens=0`；`fixed_budget` 已含 2000 又减一次，注入的 12000 字符无 token 硬裁剪 |
| 9 | `chat.py:170` 注释 + `snapshots.py:115-116` | 一次请求 4~6+ 个独立 DB session；`SnapshotMiddleware` 每请求 `load_snapshot` |
| 10 | `knowledge_router.py:135-153/160` | PDF 解析在 async SSE 生成器内同步跑，还有 `sleep(0.3)`「让前端看进度」 |
| 11 | `memory_compressor.py:116` + `token_budget.py:42-50` | `tiktoken.encode` 同步 CPU 在协程内执行 |
| 12 | 前端 `MessageBubble` | 未 `memo`，流式时每帧重跑 Markdown/KaTeX（见 `docs/代码审查与优化建议报告.md:291`） |

---

## 七、优化清单（按收益/改动比排序）

### P0：改配置 / 小改动，立刻见效

| # | 位置 | 现状 → 建议 | 收益 | 风险 |
|---|---|---|---|---|
| 1 | `config/agent.yaml:98` | `enable_summarize: false`（走 `_truncate_documents`），或仅当 context 超阈值才摘要、3 篇合并成 1 次 LLM；顺便把 `truncate_max_chars` 接进 `_truncate_documents` | 省 1~3 次 LLM，**TTFB 降 1~3s** | 长切片信息损失，建议 A/B 灰度 |
| 2 | `rag_service.py:110-114` + `vector_store.py:184/222` | 在 `query()` 开头算一次 query embedding（`asyncio.Lock` 单飞或 `embed_query`），以 `query_embeddings` 下传两路 search | 省 1 次外部 embedding 往返（100~300ms）+ 消除缓存竞态；符合 `docs/升级方案附件/10_RAG四层架构与多路召回设计.md:63` | key 需含语义空间/维度，别跨模型复用 |
| 3 | `chat.py:591/643` | 立即返回 generator，把 `gather` 移进 `generate_stream`，先 `yield thinking('retrieval')` + 定期 keep-alive 注释帧 | **TTFB 从「RAG 全链路」降到近 0**，长检索不再被代理判超时 | 需保证 `set_assistant_data` 在 `execute_agent` 之前（ContextVar 按 task 继承） |
| 4 | `config/agent.yaml:44`、`query_classifier.py:186` | 默认关 `react_l1_enable`（或仅复杂任务开）；分类 L2 加 `asyncio.timeout(3)` + 结果缓存 | 典型请求少 1~2 次 LLM，最坏省 15~45s | 质量回落，用 `scripts/reflection_stats.py` 灰度 |
| 5 | `chat.py:479` | 改成 `budget.allocate(rag_context)` 传真实字符串；在 `set_assistant_data` 用 `TokenCounter` 按 `rag_context_max` 做 token 级裁剪 | 避免超长 prompt prefill 与 `ModelContextExceeded` | 低，同步更新 `tests/test_model_context_budget.py` |

### P1：结构性改造

| # | 位置 | 建议 | 收益/风险 |
|---|---|---|---|
| 6 | `rag_service.py:73-174` | `asyncio.timeout(5.0)` 包住整个 query，内部按 1000/1500ms 给 embedding/检索/重排/摘要子预算；超时返回 `status=partial/degraded` 且保留已完成证据；重排失败退回 **RRF 排名**而非原序（`:292-296`） | P95 有上界；可见性过滤必须 fail-closed |
| 7 | `rag_service.query` 出入口 + `redis_client.py` | 增加 `rag:v2:{user_id}:{permission_epoch}:{release_id}:{index_version}:{md5(query)}:{top_k}` 结果缓存 TTL 300~600s；**命中仍执行 visibility_checker**；笔记/文档写入按 user 前缀失效 | 追问命中时检索≈0 耗时；key 必须含权限/索引版本，否则泄露已删内容 |
| 8 | `vector_store.py:98/185/223`、`knowledge_router.py:135`、`memory_compressor.py:116` | Chroma / PDF / tiktoken 用**独立有界线程池** `await asyncio.to_thread(...)`；去掉「为显示进度」的 sleep | 消除并发下事件循环卡死；别和 rerank 池互抢 |
| 9 | `rag_service.py:246-296`、`main.py:202-221` | rerank 前预截 `min(10, len)`、`predict(batch_size=...)`、可用时 `device='cuda'`、启动 warmup、加 `asyncio.wait_for` 与 `rerank_ms` 埋点 | CPU 打分降 30~60%；预截可能损失长尾 |
| 10 | `workflow_events.py:82-103`、`snapshots.py:90-136` | 进度事件批量/去抖落库；snapshot 按 `release_id` 进程内缓存 + 发布通知失效；`permit` 请求内复用 | 降低 DB 往返/锁竞争；需保 `sequence` 单调与事件补读回归 |
| 11 | `chat.py:307/395/685/872`、`main.py:94-119` | stage2 等待收敛到一处；`_init_reranker` 与 `_init_services` 并行 | 冷启动首请求省 5~30s |
| 12 | `stream.py:130-134`、`workflow_events.py:64-67`、`MessageBubble` | 服务端按 `stream_chunk_size=15` 或 ~16ms 窗口合并 `response` 帧（工具/思考帧不合并）；前端 `memo` + 滚动贴底 100ms 节流 | 帧数降一个量级，长会话掉帧消失 |

### 先做哪三件事

1. **关闭 RAG 摘要**（改一行 `config/agent.yaml:98`）+ **关 `react_l1_enable`**（`:44`）——立减 3~5 次 LLM 调用，风险最低。
2. **query embedding 单飞**——纯代码局部改动，省一次外部往返，同时修掉缓存竞态。
3. **RAG 移出 TTFB + 先发 thinking 帧**——体感提升最大，且不改变答案内容。

### 动手前先补可观测

在 `_run_rag` / `RagService.query` 各阶段打点：`rag_embed_ms`、`rag_chroma_ms`、`rag_rerank_ms`、`rag_summarize_ms`、`classify_ms`、`first_token_ms`、`llm_calls`，按 P50/P95 看板定位；否则「优化哪里」只能靠猜。设计文档 `docs/升级方案附件/10_RAG四层架构与多路召回设计.md:121-129` 也要求用 P50/P95 + Recall@20 做消融后再决定启用各层。

---

## 八、参考文档（仓库内已有结论）

- `docs/多Agent与RAG企业化架构审查_2026-11-14.md`：R9 同步 Chroma/PDF 阻塞、检索在 SSE 开始前完成、`"" * rag_context_max` 空串、HyDE 聊天已关闭；R2 reranker `compute_score` 历史静默失效；R10 缓存需绑权限/索引版本。
- `docs/升级方案附件/10_RAG四层架构与多路召回设计.md`：G3 检索总预算 5000ms / 改写 1000ms / 重排 1500ms、同一问题只 embed 一次；G6 支路失败保留有效结果、缓存命中不跳过可见性核验。
- `docs/代码审查与优化建议报告.md`：A3 `VectorStoreService` 4 处实例化；A4 超时分散、分类无超时；F9 前端 `MessageBubble` 未 memo。
- `docs/agent-call-chain.md`：Agent 主链路函数级路线（本系列上一篇）。
