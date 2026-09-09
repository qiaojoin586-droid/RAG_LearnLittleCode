# DashScope 迁移 OpenAI 大模型接入设计方案

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.3 |
| 编写日期 | 2026-09-08 |
| 适用范围 | RagLearnCode 后端（FastAPI + LangChain，`.venv` / `requirements.lock:65` 锁定 `langchain-openai 1.3.5`） |
| 目标 | 将 LLM 调用从阿里云百炼（DashScope）切换为 OpenAI 大模型接口 |
| 设计原则 | 以现有代码实现为准；新增 provider 而非破坏性替换；保留一键回退能力 |

### 修订记录

| 版本 | 日期 | 修订内容 |
| --- | --- | --- |
| v1.0 | 2026-09-08 | 初版 |
| v1.1 | 2026-09-08 | 按评审意见修订：① 修正 OpenAI 官方端点不返回思考文本的前提；② 修复 `reasoning_effort` 误注入非推理模型导致 400；③ 共享变量改为 `OPENAI_` 前缀专属变量，修复回滚失效；④ 补 pricing 币种/单位换算公式；⑤ 补 Embedding 的 `base_url` 与 `check_embedding_ctx_length`；⑥ 补 `token_budget.py`、检索质量回归、部署侧、回退验收；⑦ 新增 `create_outline_model()` 消除 ppt_service → factory 私有 API 的跨模块引用 |
| v1.2 | 2026-09-08 | 按第二轮评审修订（核对 `.venv` 内 `langchain-openai 1.3.5` 源码）：F1 §6.4 思考流扩为三选一，补 Responses API 内建路径；F2 `create_outline_model()` 显式处理 ollama；F3 缺 key 语义对齐现状；F4 风险表补限流/重试/推理延迟；F5 补单元测试计划；F6–F12 启动 warning、`trust_env` 代理、历史价格行、回滚成本、工具调用差异、Embedding 超长口径、`gpt-5-chat` 排除；P3 compose 无需修改、`qwen3.8-max` 实配说明、`dimensions` 具名参数 |
| v1.3 | 2026-09-08 | 按第三轮评审修订（深挖 Responses API 实现 + **实测 DashScope**）：**F13** 路径② `output_version` 门控分析，纠正"方案 A 让 content 回到字符串"——经核实 v0 下 content 仍为 list，故**方案 B（stream.py 遍历 content block）为必然**；**F14** 实证 `reasoning_content` 在 1.3.5 下不被提取（`additional_kwargs` 恒空），登记为既有缺陷；**F15** reasoning 值为块字典需提取文本；**F16–F20** `max_retries`、Responses 触发条件注释、步骤编号、warning 措辞、单测补 content list 用例 |

---

## 1. 背景与目标

### 1.1 背景

当前项目通过 `MODEL_PROVIDER` 环境变量一键切换模型提供商，支持 `dashscope`（阿里云百炼）与 `ollama`（本地）两种。**关键事实**：DashScope 分支并非使用 `ChatTongyi` 原生 SDK，而是已经走 **OpenAI 兼容协议**——`ChatOpenAI` + `base_url=https://dashscope.aliyuncs.com/compatible-mode/v1`（见 `app/utils/factory.py:322`）。

这意味着：切换到 OpenAI **不需要重构调用链路**，LangChain 的 `ChatOpenAI` 天然支持 OpenAI 官方端点，主要工作集中在三件事：

1. **Provider 抽象扩展**——在工厂层新增 `openai` 分支；
2. **模型映射与差异适配**——处理思考模式、Embedding 维度、视觉模型、图片计费等差异；
3. **Embedding 向量库迁移**——更换 Embedding 提供商必然导致向量维度变化，需重建 ChromaDB。

### 1.2 目标

- [ ] 通过修改 `.env` 即可将全部 LLM 能力（对话 / 深度思考 / 分类 / Plan / 视觉 / Embedding / PPT 大纲）切换到 OpenAI；
- [ ] 保留 `dashscope` / `ollama` 分支作为回退，**仅改 `MODEL_PROVIDER` 一处**即可无损切回（共享变量全部按 provider 隔离，见 §5.6）；
- [ ] 深度思考（前端"深度思考"开关）在 OpenAI 上**模型切换语义正确**，且不出现静默失效；思考过程在官方端点可经 **Responses API** 展示（可选增强，见 §6.4），并顺带修复现状思考流空转的既有缺陷（§2 #24）；
- [ ] 向量库完成维度迁移，RAG / 笔记检索功能不受影响，且检索质量经黄金评测回归确认；
- [ ] 成本账单（`pricing.yaml` → `model_pricing` 表）的模型名、币种、单位与真实价格一致。

---

## 2. 现状盘点：DashScope 接入全景

下表列出所有与 DashScope 直接耦合的位置（精确到文件与行号，核对日期 2026-09-08，行号已逐行比对真实代码）。

| # | 文件 | 行号 | 耦合内容 | 迁移动作 |
| --- | --- | --- | --- | --- |
| 1 | `app/utils/factory.py` | 71–74 | `ModelProvider` 枚举仅含 `DASHSCOPE` / `OLLAMA` | 新增 `OPENAI` |
| 2 | `app/utils/factory.py` | 94–109 | `get_provider()` 读 `MODEL_PROVIDER`，默认 `dashscope` | 保留，扩展分支 |
| 3 | `app/utils/factory.py` | 116–137 | `create_chat_model()` 按 provider 分发 | 新增 OpenAI 分支 |
| 4 | `app/utils/factory.py` | 140–158 | `create_embed_model()` 分发到 `_create_dashscope_embed_model()` | 新增 OpenAI 分支 |
| 5 | `app/utils/factory.py` | 161–209 | `create_classifier_model()`：DashScope 分支缺 key 时**回退主模型**（179–181） | OpenAI 分支对齐此降级语义（F3） |
| 6 | `app/utils/factory.py` | 212–267 | `create_vision_model()`：DashScope 分支用 `ChatOpenAI`（读无前缀 `VISION_MODEL`） | 新增 OpenAI 分支（读 `OPENAI_VISION_MODEL`） |
| 7 | `app/utils/factory.py` | 270–314 | `create_plan_model()`：DashScope 分支缺 key 时**回退主模型**（284–286） | OpenAI 分支对齐此降级语义（F3） |
| 8 | `app/utils/factory.py` | 322 | `DASHSCOPE_BASE_URL` 常量 | 保留（写死端点），OpenAI 端点走 env |
| 9 | `app/utils/factory.py` | 325–363 | `_create_dashscope_chat_model()`：`extra_body={"enable_thinking": ...}` | 新增 `_create_openai_chat_model()` |
| 10 | `app/utils/factory.py` | 366–394 | `_create_dashscope_embed_model()`：`DashScopeEmbeddings`（langchain_community） | 新增 `_create_openai_embed_model()` 用 `OpenAIEmbeddings` |
| 11 | `app/services/ppt_service.py` | 70–95 | `_get_outline_model()` 直接 `import DASHSCOPE_BASE_URL`，读 `DASHSCOPE_CHAT_MODEL` / `DASHSCOPE_API_KEY` | 改为调 factory 新增的 `create_outline_model()` |
| 12 | `app/rag/vector_store.py` | 45–46 | `EMBEDDING_BATCH_SIZE = 20`（注释标为 DashScope 单次上限） | 保留常量，注释泛化 |
| 13 | `app/rag/vector_store.py` | 157–163 | 维度漂移防护报错文案：提及 `DASHSCOPE_EMBED_MODEL`，且 **161 行硬编码脚本名 `migrate_embeddings_2560.py`** | 文案与脚本名一并泛化 |
| 14 | `app/ai_service/stream.py` | 115–127 | `on_chat_model_stream`：`accumulated += chunk.content` + 读 `additional_kwargs.reasoning_content`（qwen thinking 语义） | 改造为兼容 content 的 str/list 双形态（§5.4，F13） |
| 15 | `app/services/token_budget.py` | 52–69 | `count_image()` 按"DashScope VL 系列计价简化公式"估算图片 token | 按 OpenAI tile 制重估（§5.7） |
| 16 | `app/services/usage_service.py` | 145–160 | 成本核算：`元/1K`（149–152），未命中价格 `cost=0` **静默漏计**（153–154），返回字段 `cost_cny`（159） | pricing 换算 + 保留历史价格行 + 未命中告警（§5.5） |
| 17 | `main.py` | 131–138 | `chat_model` / `chat_model_thinking = create_chat_model(enable_thinking=...)` | 无需改动，语义由 factory 承接 |
| 18 | `config/pricing.yaml` | 11–21 | 硬编码 qwen 模型名与价格（元 / 1K tokens） | 替换为 OpenAI 模型名 + 币种单位换算（§5.5） |
| 19 | `.env` / `.env.example` | 4–9、17–32 | `MODEL_PROVIDER`、`DASHSCOPE_*`、无前缀 `CLASSIFIER_MODEL` / `PLAN_MODEL` / `VISION_MODEL` | 新增 `OPENAI_*` 专属变量（§5.6） |
| 20 | `requirements.txt` / `requirements.lock` | txt:10、27；lock:65 | `langchain-openai>=0.3.0`（lock 锁定 **1.3.5**）、`dashscope>=1.20.0` | OpenAI 无需新增依赖；`dashscope` 保留（回退用） |
| 21 | `scripts/manual/migrate_embeddings_2560.py` | 36、53 | 迁移脚本硬编码 `DashScopeEmbeddings` / `DASHSCOPE_EMBED_MODEL` | 新增通用迁移脚本 |
| 22 | `app/ai_service/chat_route.py` | 131–134 | 转发 `thinking` 事件到 SSE（纯透传，依赖上游产生） | **无需改动**，盘点登记以免误判遗漏 |
| 23 | `app/utils/config.py` | 210–217 | `get_pricing_config()` 启动时把 `pricing.yaml` 种子进 `model_pricing` 表 | 无需改动（说明定价生效机制） |
| 24 | `app/ai_service/stream.py` + `langchain-openai 1.3.5` | stream:121 | **⚠️ 既有缺陷（与本迁移无关）**：`stream.py:121` 读 `additional_kwargs["reasoning_content"]`，但 `ChatOpenAI` 在 1.3.5 下**不透传**该字段（`base.py:469-505` 仅组装 `function_call`/`tool_calls`；docstring `base.py:1-12` 明确非标准字段 "not extracted or preserved"）。**实测（2026-09-08，DashScope `qwen3.8-max` + `enable_thinking=True`）：所有 chunk 的 `additional_kwargs` 恒为空、`reasoning_content` 非空 chunk 数为 0** → 现状 DashScope「深度思考」思考流**空转** | §5.4 改造时一并修复（本次迁移的额外收益） |
| — | `docker-compose.yml` | 46–47 | backend 服务 `env_file: - .env`（仓库无 prod compose 文件） | **compose 无需修改**，新增 `OPENAI_*` 写入 `.env` 即自动进容器（§5.8） |

> 说明：`app/ai_service/agent_runner.py`、`plan_execute_agent.py`、`rag_service.py` 等均通过 `main.py` 注入的 `chat_model` / `classifier_model` / `plan_model` 实例调用，**不直接触碰 DashScope**，无需改动。这是工厂模式带来的收益。

---

## 3. DashScope 与 OpenAI 关键差异

| 维度 | DashScope（现状） | OpenAI（目标） | 影响评估 |
| --- | --- | --- | --- |
| 通信协议 | OpenAI 兼容端点 | OpenAI 原生端点（Chat Completions / Responses） | ✅ 低：同为 `ChatOpenAI` |
| 端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `https://api.openai.com/v1`（可配代理/中转） | ✅ 低：仅 base_url |
| 认证 | `DASHSCOPE_API_KEY` | `OPENAI_API_KEY` | ✅ 低：环境变量切换 |
| 主对话模型 | `qwen3.8-max`（**以 `.env` 实配为准**；`.env.example:8` 为 `qwen3.7-plus`） | 见附录 B 映射建议 | ⚠️ 中：需确认工具调用能力 |
| 深度思考 | `extra_body={"enable_thinking": bool}`（qwen3 特有） | 推理模型固有 + `reasoning_effort` / Responses `reasoning`（**仅推理模型可用**） | 🔴 高：核心差异，见 §6 |
| 思考内容返回 | 理论为 `additional_kwargs.reasoning_content`，**但 1.3.5 不透传（空转，§2 #24）** | **Responses API** 的思考摘要在 `chunk.content` 的 reasoning block（v0 下转 `additional_kwargs["reasoning"]`） | 🔴 高：见 §6.4 三选一 |
| 轻量模型 | `qwen3.7-flash` | 轻量通用模型（见附录 B） | ✅ 低 |
| Embedding | `DashScopeEmbeddings`，`qwen3.7-text-embedding` **2560 维** | `OpenAIEmbeddings`，`text-embedding-3-large` **3072 维** / `small` **1536 维** | 🔴 高：维度变化需重建向量库 |
| 视觉模型 | `qwen3.7-max`（`image_url` / 原生 `video_url`） | `gpt-4o` / `gpt-4.1`（`image_url`，**无原生视频**） | ⚠️ 中：视频继续走抽帧（现状已是 `frames` 模式） |
| 图片 token 计费 | VL 系列按长边分辨率简化公式（`token_budget.py:55`） | **tile 制**（按 512px 切片 × 每片 token + 基础值），公式完全不同 | ⚠️ 中：影响预算裁剪与成本，见 §5.7 |
| JSON mode | `response_format={"type":"json_object"}` | 原生支持 `json_object` | ✅ 低：兼容 |
| Embedding 单次上限 | 20 条/批（`vector_store.py:45`） | 单次可传数百～数千条 | ✅ 低：保留 20 亦无碍 |

---

## 4. 总体设计

### 4.1 改造策略：新增 provider，不删除 DashScope

推荐采用 **「新增 + 切换默认」** 策略，而非删除 DashScope 代码：

1. 在 `ModelProvider` 枚举新增 `OPENAI = "openai"`；
2. 新增 `_create_openai_*` 系列私有函数，与现有 `_create_dashscope_*` / `_create_ollama_*` 平行；
3. `.env` 中 `MODEL_PROVIDER=openai` 即完成切换；`dashscope` 代码保留，出问题可一键切回。

理由：改动最小、可回退、不破坏现有测试与迁移脚本；`dashscope` 依赖（`requirements.txt:27`）继续保留仅多一次安装，风险远低于破坏性替换。

> 若最终确认要**彻底移除 DashScope**，在验证稳定后，删除范围为 `factory.py` 中全部 `_create_dashscope_*` 函数 + `DASHSCOPE_BASE_URL` 常量 + `requirements.txt` 的 `dashscope` 依赖 + `migrate_embeddings_2560.py` 中的 `DashScopeEmbeddings` 导入。此步建议放在验收之后、作为独立的清理任务，见 §9。

### 4.2 目标架构

```
main.py InitManager
   └─ app/utils/factory.py  (统一工厂)
        ├─ create_chat_model(enable_thinking)
        │    ├─ provider=openai  → _create_openai_chat_model()
        │    ├─ provider=dashscope → _create_dashscope_chat_model()
        │    └─ provider=ollama   → _create_ollama_chat_model()
        ├─ create_embed_model()  → _create_openai_embed_model() / _create_dashscope_embed_model() / ...
        ├─ create_classifier_model() / create_plan_model() / create_vision_model()
        ├─ create_outline_model()   ← 新增：PPT 大纲专用（收 JSON mode，§5.1.5）
        └─ 新增统一助手：_get_api_key() / _get_base_url()（仅服务 openai/dashscope）
```

新增 provider 后，其余调用方（`chat.py`、`agent_runner.py`、`plan_execute_agent.py`、`rag_service.py`）**零改动**。

---

## 5. 详细改造方案

### 5.1 `app/utils/factory.py`（核心，改动量最大）

#### 5.1.1 枚举与常量（对应现状 71–74、322 行）

```python
class ModelProvider(str, Enum):
    DASHSCOPE = "dashscope"
    OPENAI    = "openai"      # 新增
    OLLAMA    = "ollama"

# DashScope 端点为写死常量（现状如此，保留）；
# OpenAI 端点可配（代理/中转），统一在 _get_base_url() 内读 env，不做模块级 os.getenv。
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
```

> 不在模块级写 `OPENAI_BASE_URL = os.getenv(...)`：与现有"env 一律函数内读取"的风格保持一致，且避免 `scripts/`、`alembic`、`pytest` 等未保证先 `load_dotenv` 的入口拿到空值（仅 `main.py:16-17` 保证先加载）。

#### 5.1.2 统一 key/base_url 获取（新增私有助手）

> 适用边界：以下两个助手**仅服务 `openai` / `dashscope`**（二者都走 `ChatOpenAI` + api_key + base_url）。`ollama` 走 `ChatOllama`（无需 key、base_url 读 `OLLAMA_BASE_URL`），**不经过这两个助手**——任何新增工厂函数若可能遇到 `ollama`，须先显式拦截（见 §5.1.5），禁止用 `else` 兜底把它们打进 DashScope 分支（F2）。

```python
def _get_api_key(provider: ModelProvider) -> str:
    env = "OPENAI_API_KEY" if provider is ModelProvider.OPENAI else "DASHSCOPE_API_KEY"
    key = os.getenv(env, "")
    if not key:
        raise ValueError(f"{env} 未配置，请在 .env 中设置")
    return key

def _get_base_url(provider: ModelProvider) -> str:
    if provider is ModelProvider.OPENAI:
        return os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # 函数内读 env
    return DASHSCOPE_BASE_URL
```

#### 5.1.3 新增 `_create_openai_chat_model()`

对应 `_create_dashscope_chat_model()`（325–363 行）。要点：**推理参数只在确为推理模型时注入**（否则 400）；**`gpt-5-chat` 等非推理变体须排除**（F12）；思考摘要可选走 Responses API（F1，§6.4 路径②）；误配非推理模型时启动即告警（F6/F19）。

```python
# 推理模型名前缀（gpt-5 系列 / o 系列）；用于决定是否可注入推理参数
_OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

def _is_reasoning_model(model_name: str) -> bool:
    m = model_name.lower()
    # 排除 gpt-5-chat 等非推理变体（langchain-openai base.py:1172-1173 同法区分）
    if "chat" in m:
        return False
    return m.startswith(_OPENAI_REASONING_PREFIXES)

def _create_openai_chat_model(enable_thinking: bool = False):
    from langchain_openai import ChatOpenAI

    if enable_thinking:
        # 显式配置才用推理模型；未配置则回退主对话模型（此时不注入推理参数）
        model_name = os.getenv("OPENAI_THINKING_MODEL", "").strip() or \
                     os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1")
    else:
        model_name = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1")

    kwargs = {}
    if enable_thinking:
        if _is_reasoning_model(model_name):
            effort = os.getenv("OPENAI_REASONING_EFFORT", "medium")
            if _env_bool("OPENAI_USE_RESPONSES_API", False):
                # 路径②（§6.4）：Responses API，官方端点亦可获取思考摘要。
                # 注（F17）：reasoning 非 None 本身即触发 Responses API（base.py:1755-1761），
                # use_responses_api=True 属冗余，双写仅为显式表达意图。
                kwargs["use_responses_api"] = True
                kwargs["reasoning"] = {"effort": effort, "summary": "auto"}
            else:
                # 路径①（默认）：Chat Completions，无思考文本，仅切换推理强度
                kwargs["reasoning_effort"] = effort
            # 高 effort 思考耗时长，单独放宽超时（F4）
            kwargs["request_timeout"] = float(os.getenv("OPENAI_THINKING_TIMEOUT", "300"))
        else:
            # F6/F19：误配非推理模型，启动即告警（不静默失效）
            logger.warning(
                f"thinking 模型 {model_name} 非推理模型（gpt-5*/o1*/o3*/o4* 且非 *-chat），"
                f"深度思考开关将退化为无推理链模型"
            )

    return ChatOpenAI(
        model=model_name,
        api_key=_get_api_key(ModelProvider.OPENAI),
        base_url=_get_base_url(ModelProvider.OPENAI),
        streaming=True,
        http_async_client=get_shared_async_client(),
        request_timeout=kwargs.pop("request_timeout", 120.0),
        max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "2")),   # F16：显式重试（429/瞬断）
        stream_usage=True,
        callbacks=[TRACE_CALLBACK],
        **kwargs,
    )
```

要点：
- 回退目标为 `OPENAI_CHAT_MODEL`（而非写死 `gpt-5`），消除"回退到非推理模型却带推理参数"的 400；
- 仅当模型名命中推理前缀且非 `*-chat` 变体才注入推理参数，双保险；
- 由此 `main.py:131-138` 的 `chat_model_thinking` 在未配置 `OPENAI_THINKING_MODEL` 时会成功创建为"主模型副本"（不报错、不置 `None`），前端开关退化为"同模型"，**不静默失效**；
- `reasoning_effort` 在 1.3.5 为一等字段（`base.py:787`），具名传入即可；
- **注意**：开启路径②（Responses API）后，流式 `chunk.content` 变为 **list**，必须配合 §5.4 的 stream.py 改造，否则 `accumulated += chunk.content` 会 `TypeError`（F13）。

#### 5.1.4 新增 `_create_openai_embed_model()`

对应 `_create_dashscope_embed_model()`（366–394 行），将 `DashScopeEmbeddings` 换成 `OpenAIEmbeddings`。**必须显式传 `base_url`**（否则配了中转后 Chat 走中转、Embedding 仍直连官方，国内环境必然失败），并**关闭默认的 token 分块**：

```python
def _create_openai_embed_model():
    from langchain_openai import OpenAIEmbeddings

    model_name = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-large")
    # dimensions 仅对 text-embedding-3-* 有效；换其它模型须留空，否则 400。
    # dimensions 在 1.3.5 为一等字段（embeddings/base.py:189），具名传入。
    dimensions_env = os.getenv("OPENAI_EMBED_DIMENSIONS", "").strip()

    return OpenAIEmbeddings(
        model=model_name,
        api_key=_get_api_key(ModelProvider.OPENAI),
        base_url=_get_base_url(ModelProvider.OPENAI),   # 关键：与 Chat 同一端点（中转/代理）
        check_embedding_ctx_length=False,               # 关键：见下方说明
        dimensions=int(dimensions_env) if dimensions_env else None,
    )
```

关于 `check_embedding_ctx_length=False`（默认 `True`，`embeddings/base.py:335`）：
- 默认 `True` 会把每条文本先切成 token 数组、再按约 1000 token 分块**多次发请求**；
- 这会让 `EMBEDDING_BATCH_SIZE=20`（管"条目数"）管不住实际"请求数"，写入量与耗时估算失真；
- 更重要的是，**部分中转/兼容网关不支持 token 数组入参，会静默失败**；
- 显式置 `False` 回到"传字符串"的语义（`embeddings/base.py:167/177/280` 官方推荐用法），与 DashScope 行为对齐；超长口径见 §7.4。

> ⚠️ `OpenAIEmbeddings` 来自 `langchain_openai`，`requirements.txt:10` 已声明 `langchain-openai>=0.3.0`，**无需新增依赖**。

#### 5.1.5 新增 `create_outline_model()`（PPT 大纲专用）

现状 `ppt_service.py:70-95` 是除 factory 外唯一的 DashScope 硬编码。与其让它 import factory 的**私有**助手（`_get_api_key` / `_get_base_url`，跨模块引私有 API 是坏味道），不如在 factory 增加一个公开工厂函数，把"JSON mode + 关思考"的需求一并收敛。**显式处理三种 provider，禁止 `else` 兜底**（F2）：

```python
def create_outline_model(request_timeout: float = 50.0):
    """
    创建 PPT 大纲生成模型（JSON mode，非流式，关闭思考）。

    供 ppt_service 调用，屏蔽 provider 差异：
    - openai     → ChatOpenAI(OPENAI_CHAT_MODEL, json_object)
    - dashscope  → ChatOpenAI(DASHSCOPE_CHAT_MODEL, json_object, enable_thinking=False)
    - ollama     → 不支持（本地小模型难以稳定输出 JSON 大纲），显式报错

    Raises:
        ValueError: provider 为 ollama（PPT 大纲生成暂不支持）
    """
    from langchain_openai import ChatOpenAI

    provider = get_provider()
    if provider is ModelProvider.OLLAMA:
        # F2：显式失败，避免落入 DashScope 分支误打百炼端点、报"DASHSCOPE_API_KEY 未配置"误导
        raise ValueError("PPT 大纲生成暂不支持 ollama provider（需 JSON mode 较强模型）")

    if provider is ModelProvider.OPENAI:
        model_name = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1")
        extra = {}
    else:  # dashscope
        model_name = os.getenv("DASHSCOPE_CHAT_MODEL", "qwen3-max")
        extra = {"extra_body": {"enable_thinking": False}}

    return ChatOpenAI(
        model=model_name,
        api_key=_get_api_key(provider),
        base_url=_get_base_url(provider),
        streaming=False,
        request_timeout=request_timeout,
        model_kwargs={"response_format": {"type": "json_object"}},
        **extra,
    )
```

#### 5.1.6 其余三个工厂函数（缺 key 语义对齐现状，F3）

`create_classifier_model()` / `create_plan_model()` / `create_vision_model()` 各新增一个 `ModelProvider.OPENAI` 分支，**统一读 `OPENAI_` 前缀专属变量**（隔离共享变量，见 §5.6）。

**缺 key 语义对齐 DashScope 现状**（F3）：分类 / Plan 属轻量增强，DashScope 分支缺 key 时**回退主模型**（`factory.py:179-181`、`284-286`），OpenAI 分支保持一致，避免因 `_get_api_key` 直接 `raise` 导致能力整体消失：

```python
# create_classifier_model() 的 OpenAI 分支（create_plan_model() 同理，改 OPENAI_PLAN_MODEL）
if provider == ModelProvider.OPENAI:
    model_name = os.getenv("OPENAI_CLASSIFIER_MODEL", "gpt-4.1-mini")
    if not os.getenv("OPENAI_API_KEY", ""):
        logger.warning("OPENAI_API_KEY 未配置，分类器退化为使用主模型")  # 对齐 DashScope 现状
        return create_chat_model()
    logger.info(f"创建分类器模型: {model_name} (OpenAI)")
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=model_name,
        api_key=_get_api_key(ModelProvider.OPENAI),
        base_url=_get_base_url(ModelProvider.OPENAI),
        streaming=False,
        http_async_client=get_shared_async_client(),
        request_timeout=30.0,
        callbacks=[TRACE_CALLBACK],
    )
```

- 视觉：`ChatOpenAI(model=OPENAI_VISION_MODEL, streaming=True)`，视频继续走抽帧（`multimodal_processor.py` 现状即 `frames` 模式，无需改）；
- 均**去掉** `extra_body={"enable_thinking": ...}`（OpenAI 无此参数）。

### 5.2 `app/services/ppt_service.py`（去硬编码）

`_get_outline_model()`（70–95 行）改为调 factory 公开接口，不再感知 provider：

```python
def _get_outline_model(self):
    from app.utils.factory import create_outline_model
    outline_timeout = self.config.get("outline_timeout", 45)
    return create_outline_model(request_timeout=outline_timeout + 5)
```

> OpenAI 的 `json_object` 模式同样要求 prompt 含小写 "json" 字样，现有 `_generate_outline()`（234–254 行）已满足，无需改。ollama provider 下 `create_outline_model()` 会 `raise`，`ppt_service` 现有 `_generate_outline()` 的重试/纯文本降级链路（259–285 行）可承接，不会导致服务崩溃。

### 5.3 `app/rag/vector_store.py`（文案 + 脚本名泛化）

- 45–46 行：`EMBEDDING_BATCH_SIZE = 20` 保留（OpenAI 上限更高，20 更稳），注释从"DashScope 单次上限"改为"Embedding API 单次安全上限"；
- 157–163 行：维度漂移报错文案做两处泛化——
  - 模型变量名"将 `DASHSCOPE_EMBED_MODEL` 恢复"→"将当前 provider 的 `*_EMBED_MODEL` 恢复为建库时的模型"；
  - **161 行硬编码的 `migrate_embeddings_2560.py` 脚本名** → 改为新的通用脚本 `migrate_embeddings.py`（见 §7.2），否则用户按提示会跑错脚本。

### 5.4 `app/ai_service/stream.py`（兼容 content 的 str/list 双形态，F13/F14/F15）

**现状两个问题**：
1. `accumulated += chunk.content`（`stream.py:125`）默认 `content` 是 str。开启路径②（Responses API）后 `content` 变为 **list**（text/reasoning block），`str += list` 会 `TypeError`（F13）；
2. `additional_kwargs["reasoning_content"]`（`stream.py:121`）在 1.3.5 下**拿不到**（F14，§2 #24 实测空转）。

**改造**：新增两个辅助函数，统一把 `content`（str 或 list）拆成「正文 + 思考」，并从 `additional_kwargs` 的两个键兜底（reasoning 值为块字典时需提取文本，F15）。

```python
def _reasoning_text_from_block(block) -> str:
    """从 reasoning 块提取思考文本（OpenAI Responses block / v0 additional_kwargs 兜底，F15）。"""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        summary = block.get("summary") or []
        text = "".join(p.get("text", "") for p in summary if isinstance(p, dict))
        return text or block.get("text", "") or block.get("reasoning", "")
    return ""


def _split_content(content) -> tuple[str, str]:
    """
    把 chunk.content 拆成 (正文, 思考)，兼容两种形态：
    - str：Chat Completions / DashScope / Ollama 的纯文本；
    - list[dict]：OpenAI Responses API 的 content block（text / reasoning 块，F13）。
    """
    if isinstance(content, str):
        return content, ""
    text_parts, reasoning_parts = [], []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "reasoning":
                reasoning_parts.append(_reasoning_text_from_block(block))
    return "".join(text_parts), "".join(reasoning_parts)
```

`on_chat_model_stream` 分支（对应 `stream.py:115-127`）改写：

```python
if event_type == "on_chat_model_stream":
    chunk = event["data"].get("chunk")
    if chunk:
        ak = getattr(chunk, "additional_kwargs", None) or {}
        # 思考来源①：additional_kwargs（兼容网关 reasoning_content——但 1.3.5 不透传，
        # 见 §2 #24；以及 v0 模式的 reasoning 块）
        reasoning = ak.get("reasoning_content") or _reasoning_text_from_block(ak.get("reasoning"))
        # 思考来源② + 正文：content（str 或 content-block list）
        text, content_reasoning = _split_content(getattr(chunk, "content", ""))
        reasoning = reasoning or content_reasoning
        if reasoning:
            yield {"type": "thinking", "stage": "thinking", "content": reasoning}
        if text:
            accumulated += text
            consecutive_tool_calls = 0
            yield {"type": "response", "content": text}
```

> 该改造向后兼容：Chat Completions / DashScope / Ollama 的 `content` 仍为 str，走 `_split_content` 第一分支，行为与现状一致；仅当开启路径②（Responses API，`content` 为 list）时才走块遍历分支。

### 5.5 `config/pricing.yaml`（成本账单 + 币种单位换算 + 历史价格保留）

`model_pricing` 表字段语义为 **元（CNY）/ 1K tokens**（`usage_service.py:149-152`，返回字段 `cost_cny`，`usage_service.py:159`），而 **OpenAI 官方报价为 美元（USD）/ 1M tokens**。两者**不可直接照抄**，否则账单会放大约 139 倍（少了 ÷1000、又少了 ×汇率）。

**换算公式**（必须写进配置注释）：

```
元 / 1K tokens  =  (美元 / 1M tokens) × 汇率(USD→CNY) ÷ 1000
```

**换算示例**（汇率为占位，需以采集日实际汇率为准并记录来源）：

| 模型 | 官方报价（USD/1M，占位待核） | 汇率（占位） | 换算后（元/1K） |
| --- | --- | --- | --- |
| gpt-4.1 输入 | $2.00 | 7.2 | 0.0144 |
| gpt-4.1 输出 | $8.00 | 7.2 | 0.0576 |

```yaml
# 模型定价（元 CNY / 1K tokens）
# 换算公式：元/1K = (美元/1M) × 汇率 ÷ 1000；汇率采集日期：____（来源：____）
# ⚠️ 模型名须与 ModelTrace.model 精确一致（含日期后缀）；价格按 OpenAI 官方现行价格核实后填写，禁止照抄。
# ⚠️ 保留历史 qwen 价格行：历史账单（model_traces 表存量记录）仍按旧模型名核算，删除会导致历史成本归零。
models:
  # ---- 历史 DashScope 模型（保留，供历史账单核算） ----
  qwen3.8-max:
    input_price_per_1k: 0.02
    output_price_per_1k: 0.06
  qwen3.7-max-2026-06-08:
    input_price_per_1k: 0.02
    output_price_per_1k: 0.06
  qwen3.7-flash-2026-07-15:
    input_price_per_1k: 0.0003
    output_price_per_1k: 0.0006
  # ---- OpenAI 模型（新增，按上方公式换算） ----
  gpt-4.1:
    input_price_per_1k: <按公式换算>
    output_price_per_1k: <按公式换算>
  gpt-5:                     # 深度思考 / 主模型
    ...
  gpt-4.1-mini:              # 分类 + Plan 轻量模型
    ...
  text-embedding-3-large:    # Embedding
    ...
```

**未命中价格的静默漏计（F8）**：`usage_service.py:153-154` 对未配置价格的模型 `cost=0` **不计费且不告警**。缓解：
- 新模型名必须与 `ModelTrace.model` **精确一致**（含日期后缀，如 `gpt-5-2026-xx-xx`），否则该模型成本静默为 0；
- 建议在 `usage_service.py` 的 `else` 分支补一条 `logger.warning(f"模型 {model} 未配置价格，成本按 0 计")`，把静默漏计变为可观测（低成本、高收益，属配套优化）。

> `get_pricing_config()`（`app/utils/config.py:210-217`）在应用启动时把 `pricing.yaml` 种子进 `model_pricing` 表，改后重启生效。建议将汇率写为配置项并注明采集日期，避免账单随汇率漂移。

### 5.6 `.env` / `.env.example`（共享变量按 provider 隔离）

**核心原则：OpenAI 侧全部使用 `OPENAI_` 前缀专属变量，不改动 DashScope 在用的存量共享变量**（`CLASSIFIER_MODEL` / `PLAN_MODEL` / `VISION_MODEL`）。这样切换到 OpenAI 时无需触碰 DashScope 的回退配置，回滚才真正"只改 `MODEL_PROVIDER` 一处"。

新增 OpenAI 配置段（**追加**，不删改现有 DashScope 段）：

```ini
# ========== 模型提供商 ==========
MODEL_PROVIDER=openai            # openai / dashscope / ollama

# ========== OpenAI ==========
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1   # 国内服务器需改为中转/代理地址（见 §5.8）
OPENAI_CHAT_MODEL=gpt-4.1
OPENAI_THINKING_MODEL=gpt-5                 # 深度思考（推理模型，见 §6）；留空则回退主模型
OPENAI_REASONING_EFFORT=medium              # low/medium/high，仅推理模型生效
OPENAI_USE_RESPONSES_API=false              # true=走 Responses API 以展示思考摘要（§6.4 路径②，须配合 §5.4 stream 改造）
# OPENAI_OUTPUT_VERSION=                    # 进阶：留空=responses/v1（推荐，思考摘要走 content block）；
                                            # 设 v0 则思考摘要转 additional_kwargs["reasoning"]（F13；content 仍为 list，不能省 §5.4 改造）
OPENAI_THINKING_TIMEOUT=300                 # 高 effort 思考专用超时（秒，F4）
OPENAI_MAX_RETRIES=2                        # 请求重试次数（F16）
OPENAI_EMBED_MODEL=text-embedding-3-large
OPENAI_EMBED_DIMENSIONS=3072                # 仅 text-embedding-3-* 有效；换模型须留空
OPENAI_CLASSIFIER_MODEL=gpt-4.1-mini        # 专属变量，勿改无前缀 CLASSIFIER_MODEL（留给 DashScope 回退）
OPENAI_PLAN_MODEL=gpt-4.1-mini              # 同上
OPENAI_VISION_MODEL=gpt-4o                  # 专属变量，勿改无前缀 VISION_MODEL（留给 DashScope 回退）
```

> 命名说明：DashScope 分支的轻量/视觉模型读**无前缀**的 `CLASSIFIER_MODEL` / `PLAN_MODEL` / `VISION_MODEL`（`factory.py:177/282/235`，历史遗留），而 `DASHSCOPE_CHAT_MODEL` / `DASHSCOPE_EMBED_MODEL` 带前缀——本就**不对称**。为保回退不破坏现状，OpenAI 侧统一采用 `OPENAI_` 前缀，附录 A 完整对照。

### 5.7 `app/services/token_budget.py`（图片 token 估算公式）

现状 `count_image()`（52–69 行）按"DashScope VL 系列按长边分辨率"的简化公式估算，与 OpenAI 的 **tile 制**不符。OpenAI 图片计费：先缩放至长边 ≤2048、短边 ≤768，再按 512×512 切片，**token = 切片数 × 每片定额 + 基础值**（具体定额随模型档位 `detail` 而定）。

改造建议：
- `count_image()` 增加 provider 分支：DashScope 沿用现公式，OpenAI 改按 tile 制估算；
- 该值用于 `token_budget` 的预算裁剪（`image_tokens_per_msg`、`max_history_images`）与 T10 成本核对，估算偏保守即可，但公式口径需与 OpenAI 一致，避免系统性低估/高估；
- 精确单价仍以 `pricing.yaml` 为准，`count_image` 只负责"量"的估算。

### 5.8 部署侧（`.env` 透传 + 代理注入）

**compose 无需修改（P3 修正）**：`docker-compose.yml` 的 backend 服务用 `env_file: - .env`（46–47 行），仓库内无 prod compose 文件。新增 `OPENAI_*` 只要写进 `.env` 就自动进容器，**`docker-compose.yml` 不需要任何改动**（附录 C 该行已降级为确认说明）。

**代理注入方式（F7）**：`get_shared_async_client()`（`factory.py:40-51`）创建 `httpx.AsyncClient` 时**未关闭 `trust_env`**，httpx 默认 `trust_env=True`，因此 **`HTTPS_PROXY` / `NO_PROXY` 环境变量会自动生效**，无需改代码。代理部署只需：
- 在 `.env`（经 `env_file` 透传进容器）或 compose `environment` 中配置 `HTTPS_PROXY=http://<proxy>:<port>`；
- `OPENAI_BASE_URL` 保持官方 `https://api.openai.com/v1`。

**网络可达性前置确认（决定方案可行性）**：国内服务器**直连 `api.openai.com` 基本不可用**。必须在实施前确定以下三者之一：
1. **中转/兼容网关**：将 `OPENAI_BASE_URL` 指向网关（注意 §5.1.4 的 `check_embedding_ctx_length` 与 §6.4 的思考流均受网关实现影响，需实测）；
2. **出站代理**：按上方 `HTTPS_PROXY` 方式注入，`OPENAI_BASE_URL` 保持官方；
3. **国内合规通道**：改用可合规访问的 OpenAI 模型服务。

此项不确定，整个方案不成立，故前置到 §9 步骤 0。

### 5.9 单元测试计划（F5/F20，项目已有 CI 门禁）

全文 §10 为手工端到端验收，新增 provider 属高风险改动，须补单元测试 `tests/test_factory_provider.py`（**monkeypatch env，无网络**）：

| 编号 | 用例 | 断言 |
| --- | --- | --- |
| U1 | provider 分发 | `MODEL_PROVIDER` 分别取 `openai` / `dashscope` / `ollama` 时，`create_chat_model()` 返回对应类型（`ChatOpenAI` × 2 / `ChatOllama`），且 `base_url` 正确 |
| U2 | `_is_reasoning_model` 前缀判定 | `gpt-5` / `gpt-5-pro` / `o3` / `o4-mini` → `True`；`gpt-4.1` / `gpt-4o` / **`gpt-5-chat`** → `False`（含反例） |
| U3 | 缺 key 行为 | 分类/Plan 缺 `OPENAI_API_KEY` → 降级返回主模型（对齐 F3）；chat/embed/vision 缺 key → `main.py` 捕获后优雅降级 |
| U4 | `create_outline_model` 三 provider | openai/dashscope 返回 `ChatOpenAI`（含 `json_object`）；**ollama → `raise ValueError`**（F2） |
| U5 | `_get_base_url` | 默认官方端点；`OPENAI_BASE_URL` env 覆盖生效；`dashscope` 返回写死端点 |
| U6 | thinking 参数注入 | `OPENAI_THINKING_MODEL=gpt-5` → 注入 `reasoning_effort`；`=gpt-4.1` → 不注入且告警（F6）；`OPENAI_USE_RESPONSES_API=true` → 注入 `use_responses_api`+`reasoning`；**`output_version` 留空 → 未显式设 v0（F20）** |
| U7 | stream `_split_content`（F13/F15） | str 输入 → `(str, "")`；list 输入含 `{"type":"text"}` → 正文拼接；含 `{"type":"reasoning","summary":[{"text":...}]}` → 思考提取；`_reasoning_text_from_block` 块字典 → 字符串 |

---

## 6. 深度思考模式适配（核心专项）

### 6.1 问题

现状（`factory.py:361` + `main.py:131-138`）的"深度思考"本质是 **同一模型（qwen3.8-max）的 `enable_thinking` 布尔开关**：开→模型输出推理链，关→直接作答。前端开关决定走 `chat_model` 还是 `chat_model_thinking`。

OpenAI 上**不存在** `enable_thinking` 参数，思考能力由模型本身决定：

- 非推理模型（`gpt-4o` / `gpt-4.1`）：无思考链，误传 `reasoning_effort` 会被 **`400` 拒绝**；
- 推理模型（`gpt-5` / `o3`）：思考是固有行为，由 `reasoning_effort`（`low` / `medium` / `high`）或 Responses API 的 `reasoning` 字段调节。

### 6.2 推荐方案：双模型映射 + 条件注入（改动最小，语义最贴合）

沿用现有 `chat_model` / `chat_model_thinking` 双实例结构，把 `enable_thinking` 映射为**两个不同模型**：

| 前端开关 | 现有（DashScope） | 目标（OpenAI） |
| --- | --- | --- |
| 关（默认） | `qwen3.8-max` + `enable_thinking=false` | `OPENAI_CHAT_MODEL`（如 `gpt-4.1`） |
| 开（深度思考） | `qwen3.8-max` + `enable_thinking=true` | `OPENAI_THINKING_MODEL`（如 `gpt-5`）+ 推理参数 |

**推理参数注入规则（修复 v1.0 缺陷 + F6/F12/F19）**：
- 仅当 `OPENAI_THINKING_MODEL` **显式配置**且模型名命中推理前缀（`gpt-5*` / `o1*` / `o3*` / `o4*`）且**非 `*-chat` 变体**时才注入；
- 未配置 `OPENAI_THINKING_MODEL` 时回退 `OPENAI_CHAT_MODEL`，**不注入**（避免对 `gpt-4.1` 400）；
- **误配非推理模型时启动即 `logger.warning`**（"退化为无推理链模型"），避免"用户以为在思考、实际没有"的错觉；
- 由此 `main.py:136-138` 不再因 400 把 `chat_model_thinking` 置 `None`，前端开关退化为"同模型"而非"静默失效"。

实现见 §5.1.3。`main.py` 的 `create_chat_model(enable_thinking=...)` 调用、`chat.py:224-225` 的取值逻辑**全部复用，无需改动**。

### 6.3 备选方案（如需保持"同一模型"体验）

统一使用推理模型（`OPENAI_CHAT_MODEL=gpt-5`），`enable_thinking` 映射为 `reasoning_effort`：关 → `low`，开 → `high`。代价：推理模型即使 `low` 档也有明显思考延迟与成本，普通问答变慢变贵。**不推荐**，仅作记录。

### 6.4 思考内容展示（三路径 + 实测修正，F1/F13/F14）

经核对 `.venv` 内 `langchain-openai 1.3.5` 源码并**实测 DashScope**，事实如下：

**关键源码证据**：
- `ChatOpenAI` 只针对官方 OpenAI API 规范，第三方非标准字段（`reasoning_content` 等）**不提取不保留**（docstring `base.py:1-12`），指向 OpenRouter/vLLM/DeepSeek 时应改用 provider 专用包（`ChatDeepSeek`/`ChatOpenRouter`）；
- Chat Completions 流式转换 `_convert_delta_to_message_chunk`（`base.py:469-505`）仅组装 `function_call`/`tool_calls`，**不读取 `reasoning_content`**；
- Responses API 内建支持：`use_responses_api`（`base.py:1090`）、`reasoning` 一等字段（`base.py:797`）、`*-pro`/`codex` 自动切换（`base.py:611` `_model_prefers_responses_api`）。

**实测证据（2026-09-08，DashScope `qwen3.8-max` + `enable_thinking=True`，langchain-openai 1.3.5）**：所有流式 chunk 的 `additional_kwargs` **恒为空**，`reasoning_content` 非空 chunk 数为 **0**。→ 现状 DashScope 思考流**空转**（既有缺陷，§2 #24）。

**三路径对比**：

| 路径 | 思考流 | 配置 | 说明 |
| --- | --- | --- | --- |
| ① 官方 Chat Completions（默认） | ❌ | `OPENAI_USE_RESPONSES_API=false`（默认） | 仅切换推理强度，无思考文本 |
| ② **官方 Responses API + reasoning summary** | ✅ | `use_responses_api=True` + `reasoning={"effort","summary":"auto"}`，**配合 §5.4 stream 改造** | 官方端点即可展示；思考摘要在 `chunk.content` 的 reasoning block |
| ③ 第三方兼容网关（`reasoning_content`） | ⚠️ | `OPENAI_BASE_URL` 指向网关 | **langchain 1.3.5 不透传 `reasoning_content`**，需 provider 专用包 / 自建客户端才能拿到（F14） |

**路径② 的 `output_version` 门控（F13，方案 A 纠正）**：
- `output_version` 默认 `None`（`base.py:1098-1099` 来自 `LC_OUTPUT_VERSION`），Responses 转换时 `None` 被内部改写为 **`responses/v1`**（`base.py:4785-4789`、`5037-5041`）；
- `additional_kwargs["reasoning"]` **仅当 `output_version == "v0"`** 才由 `_convert_to_v03_ai_message` 赋值（`base.py:4954`、`5237-5241` → `_compat.py:96`）；
- **但**：`response.output_text.delta` 产出的是 `{"type":"text","text":...}` 的 **dict 块**（`base.py:5051-5053`），且 `_convert_to_v03_ai_message` 转换后 `content` **仍为 list**（text 块留 dict，`_compat.py:132-136, 148`）。

**结论（对方案 A 的纠正）**：评审曾建议"方案 A：显式 `output_version='v0'` 让 content 回到字符串、改动最小"。经核实该前提**不成立**——v0 只把 reasoning 块挪进 `additional_kwargs`，text 块仍以 dict 留在 content list，**`str += content` 在路径②下无论 `output_version` 取何值都会 `TypeError`**。因此**方案 B（§5.4 改造 stream.py 遍历 content block）是必然选择，无法回避**；`output_version` 保持默认 `responses/v1`（思考摘要走 content block，与 text 块统一遍历），无需显式设为 v0。

**本期验收口径**：
- 模型切换语义为必过项（T2a）；
- 思考过程展示：路径①不展示；**路径②（官方 Responses API）作为可选增强**，开启 `OPENAI_USE_RESPONSES_API=true` 且 §5.4 改造后实测验收（T2b）；路径③因 1.3.5 不透传，**不在本期承诺**（如需，单独立项用 provider 专用包）。

---

## 7. Embedding 迁移与向量库重建

### 7.1 维度变化

| 模型 | 维度 |
| --- | --- |
| `qwen3.7-text-embedding`（现状，`.env:9`） | 2560 |
| `text-embedding-3-large` | 3072（默认） |
| `text-embedding-3-small` | 1536 |

维度不同 → ChromaDB 集合必须重建。现有 `vector_store._check_embedding_dimension()`（131–164 行）会在启动时探测到维度不一致并抛 `RuntimeError` 提示迁移，**属预期防护，不是故障**。

### 7.2 迁移方式

现有 `scripts/manual/migrate_embeddings_2560.py` 专用于 1024→2560，且硬编码 `DashScopeEmbeddings`（36 行）、`DASHSCOPE_EMBED_MODEL`（53 行）。新增 **provider 无关的通用迁移脚本** `scripts/manual/migrate_embeddings.py`：

1. 复用 `get_provider()` 判断 embedding 来源，动态选择 `OpenAIEmbeddings` / `DashScopeEmbeddings` / `OllamaEmbeddings`；
2. 迁移流程与 2560 脚本一致：读全量（ids/documents/metadatas）→ 删集合 → 重建 → 分批（20 条/批）重嵌入 → 回填；
3. 支持命令行参数 `--model` / `--dimensions` 覆盖 `.env`；
4. 同步把 `vector_store.py:161` 报错文案中的脚本名改为 `migrate_embeddings.py`。

> ChromaDB 持久化了原文（`#document`），重嵌入**无损**；迁移前务必暂停后端，避免 SQLite 文件锁冲突（沿用现有脚本约定）。

### 7.3 检索质量回归（不可忽视）

换 Embedding 会**改变相似度分布**，而以下参数是按 qwen embedding 调出来的，迁移后必须回归：

- `RAG_ROUTE_THRESHOLD=0.5`（`.env.example:87`，RAG 路由阈值）；
- `vector_store.py` 的 BM25 / 向量混合检索动态权重。

**措施**：迁移后重跑项目已有的黄金评测集（LLM-as-Judge），复核上述阈值与权重是否仍合适，必要时重新标定。此项并入 §9 步骤 7 与 §10 验收（F18 修正：回归是步骤 7，步骤 6 是向量库迁移）。

### 7.4 超长单条的实测口径（F11）

`check_embedding_ctx_length=False` 后按**整段字符串**发送，需确认单条不超模型上下文：

- OpenAI `text-embedding-3-*` 单条上限 **8191 token**；
- 须核对 `config/chroma.yaml` 的文档切分参数，保证切分后单条 ≤ 8191 token；
- 迁移脚本与 `_generate_embeddings()` 建议加**超长截断兜底**（如按字符估算保守截断），避免单条超长导致整批 400；
- Embedding 为**内容寻址**且有 LRU 缓存（`vector_store.py:77-82`），迁移后旧缓存随进程重启自动清空，无需手动处理；`EMBEDDING_BATCH_SIZE = 20` 对 OpenAI 亦安全，保留即可。

---

## 8. 风险分析与分级

| 级别 | 风险 | 影响 | 缓解措施 |
| --- | --- | --- | --- |
| 🔴 P0 | 国内服务器直连 `api.openai.com` 不可用 | **整个方案不成立** | 步骤 0 前置确认中转/代理/合规通道（§5.8），未确认不动工 |
| 🔴 P0 | Embedding 维度漂移导致 RAG/笔记检索抛异常 | 检索功能不可用 | 严格按 §7 迁移；依赖 `_check_embedding_dimension` 启动即拦截 |
| 🔴 P0 | 开启路径②后 stream.py 未改造 | `accumulated += chunk.content`（str += list）`TypeError`，流式中断 | §5.4 必须先改造 stream.py 再开启 `OPENAI_USE_RESPONSES_API`（F13） |
| 🔴 P0 | API Key 泄露（`OPENAI_API_KEY` 提交进仓库） | 密钥被盗用、产生费用 | Key 仅存 `.env`（已 `.gitignore`）；`.env.example` 只留占位 |
| 🟠 P1 | `reasoning_effort` 误注入非推理模型 | 400，`chat_model_thinking` 被置 `None` | §6.2 条件注入（显式配置 + 推理前缀 + 排除 `*-chat`） |
| 🟠 P1 | 限流 / 重试 / 推理模型延迟预算（F4） | 高 effort 思考超时、多轮工具调用触发 429 | 显式 `max_retries`（§5.1.3）；thinking 模型单独 `OPENAI_THINKING_TIMEOUT=300`；T2a 加"高 effort 不超时" |
| 🟠 P1 | 共享变量未隔离，回滚时拿 OpenAI 模型名打百炼端点 | 回滚后分类/Plan/视觉全 404 | §5.6 `OPENAI_` 前缀专属变量，不碰存量共享变量 |
| 🟠 P1 | pricing 未做币种/单位换算 / 未命中静默漏计 | 账单偏差约 139 倍 / 新模型成本为 0 | §5.5 换算公式 + 保留历史价格行 + 未命中告警 |
| 🟠 P1 | Embedding 未传 `base_url` / `check_embedding_ctx_length=True` | 配中转后 Embedding 直连官方失败 / 兼容网关静默失败 | §5.1.4 显式传 `base_url` + 置 `False` 并实测 |
| 🟠 P1 | GPT 模型工具调用行为与 qwen 有差异 | Agent 稳定性下降、幻觉工具调用 | 保持工具 schema 精简；验证 `parallel_tool_calls` 差异；评估 `strict` 结构化输出；`complex-*`/`note-*`/`mm-*` 列为黄金评测必过项（F10） |
| 🟡 P2 | 现状 DashScope 思考流在 1.3.5 空转（既有缺陷，F14） | 「深度思考」前端无思考过程展示（现状即如此） | §5.4 改造 + 路径②顺带修复（本次迁移额外收益） |
| 🟡 P2 | 图片 token 估算公式不符（DashScope VL vs OpenAI tile） | 预算裁剪与成本偏差 | §5.7 按 tile 制重估 |
| 🟡 P2 | 换 Embedding 后检索阈值/权重失配 | 检索召回质量下降 | §7.3 重跑黄金评测复核阈值 |
| 🟡 P2 | 每次切 provider 需全量重嵌（回滚非零成本） | 回滚耗时、检索中断 | §9.2 评估按 embedding 模型分集合 / 双写（F9） |
| 🟡 P2 | 视觉模型无原生视频 | 视频理解依赖抽帧 | 现状已是 `VIDEO_MODE=frames`，无影响 |
| 🟢 P3 | `dashscope` 依赖冗余 | 磁盘占用 | 验收稳定后按 §4.1 清理 |

---

## 9. 实施步骤与回滚

### 9.1 实施步骤

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| 0 | **前置确认**：申请 `OPENAI_API_KEY`；**确认网络可达方案（中转/代理/合规通道，§5.8）**；确认目标模型清单与现行价格（USD/1M） | 附录 B 落地 + 端点确定 |
| 1 | 改造 `factory.py`（§5.1，含 `create_outline_model`）、`ppt_service.py`（§5.2）、`stream.py`（§5.4）、`token_budget.py`（§5.7） | provider 分支可切换 |
| 2 | 编写单元测试 `tests/test_factory_provider.py`（§5.9）并纳入 CI | 单测门禁 |
| 3 | 更新 `.env` / `.env.example`（§5.6）、`pricing.yaml`（§5.5，含换算 + 历史价格保留）；确认 compose `env_file` 透传（§5.8，无需改 compose） | 配置就绪 |
| 4 | 编写通用迁移脚本 `migrate_embeddings.py`（§7.2），同步改 `vector_store.py:161` 脚本名 | 迁移工具 |
| 5 | 联调：`MODEL_PROVIDER=openai` 启动，跑通对话/分类/Plan/视觉/PPT/Embedding | 功能验证 |
| 6 | 暂停服务 → 迁移向量库 → 重启 → 校验维度与检索 | 向量库迁移 |
| 7 | 回归：**重跑黄金评测集复核检索阈值/权重（§7.3）+ 工具调用（`complex-*`/`note-*`/`mm-*`）** + 成本账单核对（含换算）+ 深度思考开关验证 | 验收 |
| 8 | （可选）彻底移除 DashScope 代码 | 清理 |

### 9.2 回滚

由于 OpenAI 侧全部使用 `OPENAI_` 前缀专属变量（§5.6），**回滚只需 `.env` 改回 `MODEL_PROVIDER=dashscope` 并重启，代码零回退**——DashScope 分支继续读它原用的 `DASHSCOPE_*` 与无前缀 `CLASSIFIER_MODEL` / `PLAN_MODEL` / `VISION_MODEL`（这些值全程未被改动）。

**例外（向量库，非零成本，F9）**：若已执行 §7 向量库迁移（维度已变为 OpenAI 维度），回滚需把 `DASHSCOPE_EMBED_MODEL` 恢复并**再次全量重嵌**回 2560 维——每次切换 provider 都是一次全量重嵌，成本高且检索中断。若需频繁切换或真·一键回退，建议评估：
- **按 embedding 模型分集合**：集合名配置化（如 `notes_openai` / `notes_qwen`），各 provider 读写各自集合，切换免重嵌；
- 或**双写**：写入时同时写两套向量（成本翻倍，换取零切换成本）。

本期按"单集合 + 全量重嵌"实现（简单、满足一次性迁移），分集合/双写作为后续优化项登记。

---

## 10. 验收测试

| 编号 | 用例 | 通过标准 |
| --- | --- | --- |
| T1 | 普通对话 | 流式输出正常，token 统计正确 |
| T2a | 深度思考开关（**模型切换语义，必过**） | 开 → `OPENAI_THINKING_MODEL`；关 → `OPENAI_CHAT_MODEL`；未配 `OPENAI_THINKING_MODEL` 时退化为同模型且告警（F6）；**高 `reasoning_effort` 下不因超时失败（F4）** |
| T2b | 思考过程展示（**可选增强，F1/F13**） | 开启 `OPENAI_USE_RESPONSES_API=true` 且 §5.4 stream 改造后：前端逐 token 展示思考摘要（从 content 的 reasoning block 提取），正文 text 块正常流出、无 `TypeError`；路径①标 N/A |
| T3 | 查询分类（L2） | `OPENAI_CLASSIFIER_MODEL` 生效，复杂/简单分流正确 |
| T4 | Plan-and-Execute | `OPENAI_PLAN_MODEL` 生成 JSON 计划，多步执行/综合正常 |
| T5 | 图片附件 | `OPENAI_VISION_MODEL` 正确理解图片 |
| T6 | 视频附件 | 抽帧 → 视觉模型理解正常（非原生视频） |
| T7 | 笔记 RAG 检索 | 迁移后维度一致，检索命中正常；**黄金评测复核 `RAG_ROUTE_THRESHOLD` 与混合权重** |
| T8 | 笔记向量写入 | 新笔记 upsert 成功，维度与集合一致；Embedding 请求经所选端点成功（验证 `check_embedding_ctx_length=False` 生效）；超长单条有截断兜底（F11） |
| T9 | PPT 生成 | `json_object` 大纲生成、解析、降级链路正常（经 `create_outline_model`）；ollama provider 下显式报错不崩溃（F2） |
| T10 | 成本账单 | `model_pricing` 表模型名与 `ModelTrace.model` 精确一致；**金额按 §5.5 公式（USD/1M × 汇率 ÷ 1000）核算正确**；历史 qwen 账单仍正常（旧价格行保留）；未命中价格有告警（F8） |
| T11 | **回退验证** | `.env` 改回 `MODEL_PROVIDER=dashscope` 重启后，对话/分类/Plan/视觉全链路可用（共享变量未被污染） |
| T12 | **缺 Key 降级（F3）** | 未配 `OPENAI_API_KEY` 时：分类/Plan **降级为使用主模型**（对齐 DashScope 现状）；chat/vision 由 `main.py` 捕获置 `None` 优雅降级，服务不崩溃 |
| T13 | **单元测试** | `tests/test_factory_provider.py`（§5.9，U1–U7）全部通过并纳入 CI |

---

## 附录 A：环境变量对照表

> 命名说明：DashScope 侧为历史遗留的**不对称命名**（`DASHSCOPE_CHAT_MODEL` / `DASHSCOPE_EMBED_MODEL` 带前缀，`CLASSIFIER_MODEL` / `PLAN_MODEL` / `VISION_MODEL` 无前缀）；OpenAI 侧**统一 `OPENAI_` 前缀**。切换/回滚均不改动对方 namespace 的变量，实现单向隔离。

| 用途 | DashScope（现状，保持不动） | OpenAI（新增） |
| --- | --- | --- |
| 提供商开关 | `MODEL_PROVIDER=dashscope`（实际 `.env` 值；`.env.example:4` 为空，靠 `get_provider()` 兜底为 dashscope） | `MODEL_PROVIDER=openai` |
| API Key | `DASHSCOPE_API_KEY` | `OPENAI_API_KEY` |
| Base URL | 硬编码 `compatible-mode/v1` | `OPENAI_BASE_URL`（默认官方，可配代理） |
| 主对话模型 | `DASHSCOPE_CHAT_MODEL` | `OPENAI_CHAT_MODEL` |
| 深度思考模型 | （同主模型 + enable_thinking） | `OPENAI_THINKING_MODEL` + `OPENAI_REASONING_EFFORT` + `OPENAI_USE_RESPONSES_API` + `OPENAI_THINKING_TIMEOUT` + `OPENAI_OUTPUT_VERSION`（进阶） |
| Embedding | `DASHSCOPE_EMBED_MODEL` | `OPENAI_EMBED_MODEL` + `OPENAI_EMBED_DIMENSIONS` |
| 分类模型 | `CLASSIFIER_MODEL`（**无前缀，共享**） | `OPENAI_CLASSIFIER_MODEL`（专属） |
| Plan 模型 | `PLAN_MODEL`（**无前缀，共享**） | `OPENAI_PLAN_MODEL`（专属） |
| 视觉模型 | `VISION_MODEL`（**无前缀，共享**） | `OPENAI_VISION_MODEL`（专属） |
| 思考开关 | `PLAN_ENABLE_THINKING` / `CLASSIFIER_ENABLE_THINKING` | **废弃**（由 `OPENAI_REASONING_EFFORT` / Responses `reasoning` 取代） |

## 附录 B：模型映射建议表

> ⚠️ 以下为**建议映射**，模型名与价格会随 OpenAI 发布迭代，**上线前必须按 OpenAI 官方现行模型清单与价格核实**（价格为 USD/1M，需按 §5.5 公式换算为 CNY/1K），本文不承担模型名/价格准确性。

| 角色 | DashScope（现状） | OpenAI 建议 | 说明 |
| --- | --- | --- | --- |
| 主对话 | `qwen3.8-max` | `gpt-4.1` | 通用能力强、成本均衡；追求上限可用推理模型 |
| 深度思考 | `qwen3.8-max` + thinking | `gpt-5`（`reasoning_effort=medium`，或 Responses `reasoning.summary`） | 推理模型；思考摘要展示见 §6.4 路径② |
| 分类（L2） | `qwen3.7-flash` | `gpt-4.1-mini`（或 `gpt-4o-mini`） | 轻量、低延迟、低成本 |
| Plan 生成 | `qwen3.7-flash` | `gpt-4.1-mini` | 同上，JSON 输出稳定 |
| 视觉 | `qwen3.7-max` | `gpt-4o`（或 `gpt-4.1`） | 支持 `image_url` |
| Embedding | `qwen3.7-text-embedding`（2560 维） | `text-embedding-3-large`（3072 维） | 或 `-small`（1536 维）以降本 |

## 附录 C：改动文件清单

| 文件 | 动作 | 优先级 |
| --- | --- | --- |
| `app/utils/factory.py` | 修改：新增 `OPENAI` 分支 + `_create_openai_*` + `create_outline_model()` + 助手函数 | P0 |
| `app/services/ppt_service.py` | 修改：`_get_outline_model()` 改调 `create_outline_model()` | P0 |
| `app/ai_service/stream.py` | 修改：新增 `_split_content` / `_reasoning_text_from_block`，`on_chat_model_stream` 兼容 content str/list（**路径②前置**，F13） | P0 |
| `app/services/token_budget.py` | 修改：`count_image()` 增加 OpenAI tile 制分支 | P2 |
| `app/rag/vector_store.py` | 修改：文案 + 161 行脚本名泛化 | P2 |
| `app/services/usage_service.py` | 修改：未命中价格补 `logger.warning`（F8 配套优化） | P2 |
| `config/pricing.yaml` | 修改：新增 OpenAI 模型（换算）+ 保留历史 qwen 价格行 | P1 |
| `.env` / `.env.example` | 修改：新增 `OPENAI_*` 专属变量 | P0 |
| `tests/test_factory_provider.py` | 新增：provider 单测（§5.9，U1–U7） | P1 |
| `scripts/manual/migrate_embeddings.py` | 新增：通用维度迁移脚本 | P0 |
| `docker-compose.yml` | **无需修改**（backend `env_file: .env` 自动透传，§5.8）；仅需确认 `.env` 含 `OPENAI_*` | — |
| `app/ai_service/chat_route.py` | 无需改动（thinking 透传，§2 #22 登记） | — |
| `main.py` | 无需改动（已通过 factory 解耦） | — |

---

## 评审记录（v1.2 → v1.3）

本节记录第三轮评审的核对结论与落实情况（评审人深挖 `langchain-openai 1.3.5` Responses API 实现；本版新增**实测**佐证）。

**本轮必须修正（F13、F14）**：

| 编号 | 问题 | 核实证据 / 落实 |
| --- | --- | --- |
| F13（P0） | 路径②默认配置拿不到思考摘要，且 `chunk.content` 为 list 会破坏现有流式 | 已核实 `output_version` 门控（`base.py:1098-1099` 默认 None → `4785-4789/5037-5041` 改写 `responses/v1`；`additional_kwargs["reasoning"]` 仅 `v0` 经 `_compat.py:96` 赋值；`base.py:5194-5224` 默认走 content block）。**并纠正评审推荐的方案 A**：`response.output_text.delta` 产 dict 块（`base.py:5051-5053`）、`_convert_to_v03_ai_message:148` 转换后 content 仍为 list，故 `output_version="v0"` **无法**让 content 回到字符串，`str += content` 在路径②下必 `TypeError`。→ 采用**方案 B**（§5.4 stream.py 遍历 content block），`output_version` 保持默认；`OPENAI_OUTPUT_VERSION` 登记为进阶项（§5.6）。落实于 §5.4 / §6.4 / §8 / T2b / U7 |
| F14（P1） | `reasoning_content` 在锁定版本根本不被提取，现状思考流疑似空转 | 已核实 `_convert_delta_to_message_chunk`（`base.py:469-505`）不读 `reasoning_content`，docstring 三处声明 "not extracted or preserved"（`base.py:1-12/628/2620`）；`requirements.lock:65` 锁定 1.3.5。**实测（2026-09-08，DashScope `qwen3.8-max` + `enable_thinking=True`）：所有 chunk `additional_kwargs` 恒空、`reasoning_content` 非空数为 0**，证实现状空转。登记为「既有缺陷（与本迁移无关）」（§2 #24），路径③加限定"langchain 1.3.5 不透传，需 provider 专用包/自建客户端"（§6.4），修复列为本次迁移额外收益 |

**建议补充（F15–F20）**：F15 reasoning 值为块字典，§5.4 `_reasoning_text_from_block` 提取文本（`summary[*].text` 兜底 `text`/`reasoning`），U7 补"块字典→字符串"；F16 §5.1.3 补 `max_retries=int(os.getenv("OPENAI_MAX_RETRIES","2"))`；F17 §5.1.3 注释"`reasoning` 非 None 即触发 Responses API（`base.py:1755-1761`），`use_responses_api=True` 双写仅为显式表达意图"；F18 §7.3 "步骤 6"改"步骤 7"；F19 §5.1.3 F6 分支 warning 措辞改"退化为无推理链模型"；F20 §5.9 U6 补 `output_version` 留空断言 + 新增 U7（content str/list 处理）。

**结论**：v1.3 已落实 F13–F20，并对方案 A 作出基于源码核实的纠正；路径②的前置条件（先改 stream.py 再开 Responses API）已写入 §8 P0 风险与 §5.1.3 注意项。方案可进入实施。
