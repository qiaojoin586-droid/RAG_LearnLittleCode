# 前端主站提问 → 后台 Agent 执行：函数调用路线

> 全链路为 HTTP POST + SSE（`text/event-stream`），无 WebSocket；后台 Agent 由 LangGraph StateGraph 编排，复杂度决定走 ReAct 还是 Plan-and-Execute。

## 一、一条主调用路线（函数级）

```
[前端]
AIAssistantPanel.tsx  onKeyDown(Enter)/onClick
  → useChatComposer.ts  handleSend()
  → useSSE.ts  start(endpoints.chat.query, body)
  → chatStream.ts  runChatStream() → consumeStream() → fetch('POST /api/v1/chat/query', Bearer jwt)
                                            ↓ nginx /api/ → FastAPI:8000
[后端入口]
main.py  app.include_router(chat.router, prefix='/api/v1')
  → chat.py  chat_query()
      ├─ get_current_user_id() → decode_token()          # 鉴权
      ├─ rate_limit() → check_rate_limit()               # 限流
      ├─ _acquire_sse_slot()                            # SSE 并发槽
      ├─ _persist_chat_turn() → get_or_create_session() → session_manager.add_message() → cache_message()
      └─ _prepare_chat_stream()
[准备层]（RAG / 记忆 / prompt）
            ├─ asyncio.gather(_run_rag(), _run_memory_compression())
            │    ├─ _run_rag() → RagService.query() → vector_store.search_documents()/search_notes()
            │    │              → reciprocal_rank_fusion() → filter() → _rerank() → context/sources
            │    └─ _run_memory_compression() → MemoryCompressor.build_context() → check_and_summarize()
            ├─ load_prompt('main') + context_composer.set_assistant_data()   # 组装 system_prompt
            └─ 返回内部生成器 generate_stream()
[编排层]
      → chat.py  generate_stream()
            ├─ ChatWorkflow.start() / WorkflowJournal() / heartbeat()
            ├─ build_attachment_blocks_async()                    # 附件多模态（有附件时）
            ├─ ChatRouteContext(...)                              # 注入模型与工具依赖
            └─ chat_graph.py  stream_chat_graph(ctx)
                  → build_chat_graph() → StateGraph: START → classify → (react | plan) → END
                  → _run() → graph.ainvoke(state)
                        → classify_node() → QueryClassifier.classify()  # L1 规则 → L2 LLM
                        → route_after_classify() → decide_route()       # simple→react / complex→plan_execute

[分支 A：简单问题 ReAct]
                        → react_node() → chat_route.py  react_events()
                              → agent_runner.py  execute_agent()
                                    → resolve_tool_groups()
                                    → create_agent_tools() → ToolRegistry.resolve_names()
                                    → AgentFactory.create_agent() → langchain.agents.create_agent()
                                    → compose_history() → agent_input={'messages':[...]}
                                    → stream.py  run_agent_stream()
                                          → mw.trigger('before_agent')
                                          → agent.astream_events(v2)   # LLM ↔ Tool 交替循环
                                                ├─ on_chat_model_stream → thinking / response 事件
                                                ├─ on_tool_start        → tool_start 事件
                                                └─ on_tool_end          → tool_end / tool_file 事件
                                          → stream_done(full_response)
                              → reflection.build_repair_note() / critique_answer()  # 失败修复 + 结果自检

[分支 B：复杂问题 Plan-and-Execute]
                        → plan_node() → chat_route.py  plan_events()
                              → plan_execute_agent.py  execute_plan_agent()
                                    → _generate_plan()            # planner 模型
                                    → _topological_batches()      # 依赖拓扑分批
                                    → _execute_batch() → _execute_step() → _execute_step_impl()
                                          → execute_agent(override_groups=...)   # 每步独立 ReAct Agent
                                    → _synthesize() / _synthesize_with_reflection() → critique_answer()
                                    → plan_complete()
                              └─ plan_fallback → react_events()  # 失败降级回分支 A

[回流与落库]（事件沿原路反向冒泡）
      → chat.py  generate_stream(): 逐事件 yield await journal.live_frame(event)
      → workflow_events.py  event_frame() 生成 `data: {...}\n\n` SSE 帧
      → chatStream.ts  deliver() → sseParser.ts  dispatchSSEEvents() → useChatComposer.ts 回调
            （onThinking / onResponse / onTool* / onPlan* / onDone 更新 UI）
      → 后端收尾：chat_service.save_message_with_commit() → workflow.succeed_in()
            → MemoryJobs.record_success() → 发送 done{session_id, workflow_id, sources}
            → chat_service.generate_and_update_title()（done 之后异步）
```

## 二、关键顺序要点

1. **RAG 在 Agent 之前完成**：`_prepare_chat_stream` 内 `_run_rag` 与 `_run_memory_compression` 并行（`asyncio.gather`），检索结果由 `context_composer.set_assistant_data` 以「低信任用户数据」注入 messages，而不是走 `/chat/rag` HTTP 接口。
2. **模型选择也在准备期**：附件→vision 模型；`enable_thinking`→chat_thinking；否则主模型（`chat.py:315-338`）。
3. **LangGraph 是编排中枢**：`stream_chat_graph` 用 `asyncio.Queue` 做事件总线，后台任务 `graph.ainvoke` 产出事件，主协程 drain 并 yield；`error` 事件后立即停止（`chat_graph.py:144-213`）。
4. **两条执行路径**：简单问题 `react_events → execute_agent`（单次 ReAct 循环，`max_iterations=10`、`max_consecutive_tool_calls=6`）；复杂问题 `plan_events → execute_plan_agent`（Plan → 拓扑分批 Execute（每步一个专用 ReAct Agent）→ Synthesize → Reflect），可 `plan_fallback` 降级为 ReAct。
5. **ReAct 内核**：`run_agent_stream` 基于 `agent.astream_events(version="v2")` 消费 `on_chat_model_stream / on_tool_start / on_tool_end`，把 LLM 与工具的交替执行翻译成 SSE 事件（`stream.py:70-250`）。
6. **先落用户消息、后落助手回复**：`_persist_chat_turn` 先写 user 消息；回复仅在流成功结束后由 `save_message_with_commit` 落库，最后才发 `done`；`error` 不发 done、不保存回复。
7. **断线续跑分支**：`continue_on_disconnect=true` 时 `_persist_chat_turn → enqueue_chat()` → `_queued_chat_frames()` ← `DetachedChatWorker.tick() → _prepare_chat_stream()`；前端断线后 `chatStream.ts:replay` 轮询 `GET /chat/workflows/{id}/events` 补读。

## 三、关键文件索引

| 层次 | 文件 |
|---|---|
| 前端入口 | `front/src/components/layout/AIAssistantPanel.tsx`, `front/src/hooks/useChatComposer.ts`, `front/src/hooks/useSSE.ts`, `front/src/utils/chatStream.ts`, `front/src/utils/sseParser.ts`, `front/src/api/endpoints.ts` |
| 后端路由 | `main.py`, `app/routers/chat.py`, `app/schemas/chat.py`, `app/utils/auth_utils.py`, `app/core/rate_limit.py` |
| 准备层 | `app/rag/rag_service.py`, `app/services/memory_compressor.py`, `app/ai_service/context_composer.py`, `app/ai_service/multimodal_processor.py`, `prompts/main_prompt.txt` |
| 编排层 | `app/ai_service/chat_graph.py`, `app/ai_service/query_classifier.py`, `app/ai_service/chat_route.py`, `app/ai_service/workflow_events.py`, `app/ai_service/workflows.py` |
| Agent 内核 | `app/ai_service/agent_runner.py`, `app/ai_service/agent.py`, `app/ai_service/agent_tools.py`, `app/ai_service/tool_registry.py`, `app/ai_service/stream.py`, `app/ai_service/reflection.py`, `app/ai_service/plan_execute_agent.py`, `config/agent.yaml` |
| 收尾 | `app/services/chat_service.py`, `app/ai_service/detached_worker.py`, `app/user_memory/jobs.py` |
