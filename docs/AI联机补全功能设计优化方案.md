# AI 联机补全（Inline Autocomplete）功能设计优化方案

> **版本**: v1.5  
> **日期**: 2026-08-26  
> **状态**: v1.6 —— **附录 G 评审项全部闭环**（F1–F18 已修或判定 N/A，G.3 无待处理项）；后端 MVP 与前端方案 A（光标处内联 ghost text）均已实施并端到端验证；门禁：后端 25 条补全单测 / 前端 81 条全绿；**唯一待办：IME 真机验证（附录 H）**  
> **关联代码**: `note_router.py` · `note_service.py` · `note.py`(schema) · `auth_utils.py` · `factory.py` · `main.py` · `prompt.yaml` · `pricing.yaml` · `NoteEditor.tsx` · `GhostTextArea.tsx`(新) · `notes.ts`

---

## 1. 现状分析

### 1.1 已有骨架代码

项目中已预留 AI 联机补全的完整骨架，但均为 **TODO 占位**，无实际 LLM 调用：

| 层 | 文件 | 现有代码 | 状态 |
|---|------|---------|------|
| **Schema** | `app/schemas/note.py:94-97` | `AutocompleteRequest(content, cursor_position)` | ✅ 可用，需扩展 |
| **Router** | `app/routers/note_router.py:310-317` | `POST /note/autocomplete` → 返回硬编码占位文本 | 🔧 需接入 LLM |
| **Service** | `app/services/note_service.py:9` | 模块注释提及「AI 内联补全」 | 🔧 无对应方法 |
| **Prompt** | `config/prompt.yaml:6` | `autocomplete: "prompts/autocomplete_prompt.txt"` | ❌ 文件不存在 |
| **前端 API** | `front/src/api/notes.ts:63-65` | `autocomplete(data)` 已封装 | ✅ 可用 |
| **前端端点** | `front/src/api/endpoints.ts:55` | `autocomplete: /api/v1/note/autocomplete` | ✅ 路径已注册 |
| **前端类型** | `front/src/types/api.ts` | 无 `AutocompleteRequest` / `AutocompleteResponse` 类型 | ❌ 需新增 |
| **编辑器** | `front/src/pages/NoteEditor.tsx` | 原生 `<textarea>` + Markdown 预览分栏 | 🔧 需集成 ghost text |

### 1.2 可复用的 AI 基础设施

| 设施 | 文件 | 复用方式 |
|------|------|---------|
| **模型工厂** | `app/utils/factory.py` | 新增 `create_autocomplete_model()` 轻量模型（与 `create_classifier_model()` 同策略，使用 qwen3.7-flash） |
| **Prompt 加载** | `app/utils/prompt_loader.py` | `load_prompt("autocomplete")` / `format_prompt("autocomplete", ...)` |
| **模型 Trace** | `app/core/model_trace.py` | `TRACE_CALLBACK` + `set_trace_stage("autocomplete")` 自动记录调用 |
| **后台任务** | `app/core/task_runner.py` | 无需（补全为同步请求-响应，非后台任务） |
| **限流** | `app/core/rate_limit.py` | `rate_limit(endpoint_limit=10)` 复用令牌桶 |
| **用量计费** | `app/services/usage_service.py` + `model_trace` | 通过 `TRACE_CALLBACK` 自动记录 token 用量到 `model_trace` 表 |
| **共享 HTTP 连接池** | `app/utils/factory.py:get_shared_async_client()` | 复用 `httpx.AsyncClient`（20 keepalive / 100 max） |
| **BackgroundInitManager** | `main.py` | 补全模型在阶段 1 初始化（与 chat_model 并行） |

### 1.3 当前编辑器架构

```
NoteEditor.tsx（三栏笔记页右侧详情区）
    │
    ├─ 标题输入框（<input>）
    ├─ 标签/分类/置顶栏
    └─ 编辑区（按文档类型分支）
        │
        ├─ txt 模式 → 单栏 <textarea>
        └─ md 模式 → 分栏（编辑 | 预览）
            ├─ 左栏 <textarea>（Markdown 源码）
            └─ 右栏 <ReactMarkdown>（实时渲染）
```

**关键约束**：编辑器使用原生 `<textarea>` 而非 Tiptap（尽管 `package.json` 已安装 `@tiptap/react` 等依赖但未使用）。ghost text 方案必须基于 `<textarea>` 实现，或评估是否引入 Tiptap 替换。

---

## 2. 交互设计

### 2.1 触发条件

| 参数 | 值 | 说明 |
|------|-----|------|
| **停顿延迟** | 600ms | 用户停止键盘输入后 600ms 触发（兼顾响应速度与不打断思路） |
| **最小字符数** | 10 | 光标前内容 ≥ 10 字符才触发（避免空白笔记/单字触发无意义请求） |
| **最大请求频率** | 10 次/分钟/用户 | 后端限流（Redis 计数器，与现有 `rate_limit` 机制一致；⚠️ S3 修复：需先修复 `get_current_user_id` 写入 `request.state.user_id`，否则按 IP 计数） |
| **取消前序请求** | 是 | 新请求发出前自动取消上一次未完成的请求（`AbortController`） |
| **最大上下文长度** | 前 800 + 后 300 字符 | 前端在光标附近截取（I4 修复；F8：与后端 `AUTOCOMPLETE_MAX_CONTEXT=800` 对齐，不再多传） |
| **最大补全长度** | 200 字符（`AUTOCOMPLETE_MAX_COMPLETION`） | 模型返回补全文本上限（超长截断）；**F5：`max_tokens` 是 token 上限，字符契约由后处理保证** |

### 2.2 按键行为

| 按键 | 行为 | 条件 |
|------|------|------|
| **Tab** | 采纳 ghost text → **插入到补全对应的光标位置**，光标移至插入点之后 | ghost text 可见时 |
| **Escape** | 丢弃 ghost text，继续编辑 | ghost text 可见时 |
| **继续输入** | 丢弃 ghost text，重置停顿计时器 | ghost text 可见时 |
| **方向键/鼠标点击** | 丢弃 ghost text（光标位置变化，上下文已失效） | ghost text 可见时 |
| **Backspace** | 丢弃 ghost text，重置计时器 | ghost text 可见时 |
| **Enter** | 丢弃 ghost text（换行后上下文变化大） | ghost text 可见时 |

### 2.3 Ghost Text 渲染方案

**方案选型**：`<textarea>` 原生不支持 ghost text。有三种实现路径：

| 方案 | 原理 | 优点 | 缺点 |
|------|------|------|------|
| **A. 覆盖层（Overlay）** | 透明 textarea + 底层 div 渲染带 ghost text 的内容 | 视觉效果最佳，ghost text 与正文无缝衔接 | 需要精确同步滚动/光标位置，实现复杂度高 |
| **B. 测量 div + 绝对定位** | 隐藏 div 测量光标像素坐标，在该位置绝对定位 ghost text span | 不改变编辑器结构，兼容性好 | 光标坐标计算需处理字体/行高/换行等边界，精度有限 |
| **C. 状态栏提示** | 在编辑器底部状态栏显示「按 Tab 采纳补全」+ ghost text 预览 | 实现最简单，零 UI 侵入 | 用户体验弱，不如内联直观 |

**✅ 选定：方案 A（覆盖层 / Overlay）**——2026-08-26 决策，见附录 F。

> v1.2 曾选方案 B，但 v1.3 的 C8 指出它与 N2 的 FIM 中部补全互斥（ghost text 会遮挡光标后的正文）。方案 A 让 ghost text **真正撑开后续文字**，是唯一能兑现"中部补全"承诺的路径。

```
┌── editor-wrapper (position: relative) ───────────────────┐
│                                                           │
│  ┌── ① highlighter div (absolute inset-0, aria-hidden) ─┐ │
│  │   {光标前正文}[ghost text 灰色]{光标后正文}            │ │  ← 用户看到的文字
│  │   ↑ 正文与 ghost 同层排版，ghost 真正撑开后续文字      │ │
│  └───────────────────────────────────────────────────────┘ │
│                                                           │
│  ┌── ② textarea (absolute inset-0, color:transparent) ──┐ │
│  │   {完整正文，文字透明，仅 caret 与选区可见}            │ │  ← 用户实际操作的层
│  └───────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
        ②在上（接收输入/选区/滚动），①在下（负责显示）
```

**为什么这样能对齐**：光标**之前**的文本在两层中排版完全一致 → caret 位置天然精确，无需任何像素测量（C6 的坐标计算问题随之消失）。光标**之后**的文本在 ① 中被 ghost 推移、在 ② 中原地不动，但 ② 的文字是透明的，用户看不到差异。

**实现要点**：

1. **两层盒模型必须逐像素一致**——共用同一个 CSS class 承载 `font-family / font-size / line-height / letter-spacing / padding / border / box-sizing / tab-size / white-space: pre-wrap / overflow-wrap: break-word`。任何一项不一致都会导致换行位置漂移。
2. **textarea 透明化**：`color: transparent; caret-color: var(--color-text);`（Safari 另需 `-webkit-text-fill-color: transparent`）。
3. **选区必须半透明**：textarea 在上层，其 `::selection` 背景会盖住下层文字。必须 `textarea::selection { background: rgba(59,130,246,.3); }` 让底层文字透出（react-simple-code-editor 同款做法）。
4. **滚动同步**：`onScroll` 中同步 `div.scrollTop = ta.scrollTop; div.scrollLeft = ta.scrollLeft`，div 设 `overflow: hidden`。**不再 `dismiss()`**（v1.2 的低优 2 作废）。
5. **滚动条宽度**：textarea 出现纵向滚动条会压缩其内容宽度，而 div 无滚动条 → 换行位置漂移。两层统一 `scrollbar-gutter: stable`。
6. **尾行塌陷**：textarea 中末尾 `\n` 会渲染出一个空行，div 中会被塌陷。div 内容需补一个尾随字符（`content + '\n'` 或 `​`）。
7. **🔴 IME 预编辑串不可见（头号风险）**：`color: transparent` 会让拼音候选阶段的预编辑文字**一并透明**，中文输入时用户看不见自己在打什么。必须在 `compositionstart` 时把 textarea 恢复不透明并隐藏 ① 层，`compositionend` 再切回。详见 §4.3。
8. ghost text 样式：`color: var(--color-text-tertiary); opacity: .5;`（在 ① 层内，无需 `pointer-events`——① 层整体 `pointer-events: none`）。

---

> **✅ C8（已决策 2026-08-26）：选 A-覆盖层**——原问题与备选方案记录如下，供追溯。
>
> **原问题**：方案 B 的 ghost text 是**绝对定位 span 叠加**在 textarea 之上，而 textarea 里**光标之后的正文仍在原位渲染**。两者会重叠：
>
> ```
> 光标在文末（唯一无冲突场景）：
>   今天学了 RAG 的召回策略▌[AI 补全内容]          ← 右侧是空白，正常显示
>
> 光标在文档中部（N2/I4 重点优化的场景）：
>   今天学了 RAG 的召回策略▌，明天继续看重排序。
>                          [AI 补全内容盖住这里]   ← 直接遮挡用户已有文字
> ```
>
> 这是个设计层面的死结：**N2 花大力气加 `context_after` 正是为了让中部补全更准，而补得越准、越长，遮挡就越严重。** v1.2 未识别此冲突。三个可选出路：
>
> | 选项 | 做法 | 代价 |
> |------|------|------|
> | B-限定 | 仅当"光标后到行尾为空白"时内联显示 ghost text；否则不显示 | ❌ 未选：`context_after` 在多数场景退化为"（无）"，N2 收益打折 |
> | **✅ A-覆盖层（选定）** | 透明 textarea + 底层 div 渲染「正文 + ghost text + 正文」，ghost text 真正**撑开**后续文字 | 需处理滚动/选区/IME 同步；**实测评估后工作量修正为 +6~8h**（原估 4~6h 未计入 IME 预编辑透明问题与滚动条宽度漂移） |
> | C-状态栏 | 完全放弃内联，补全建议只在编辑器底部状态栏展示 | ❌ 未选：体验最弱 |
>
> **决策理由**：本项目是中文笔记应用，中部续写是高频场景；方案 A 是唯一能让 `context_after`（N2 的 FIM 设计）产生实际价值的路径。代价是前端复杂度上升，已在 §4.3 逐条列出必须处理的 8 个边界。

### 2.4 加载态指示

- **请求中**：在 ghost text 位置显示闪烁的 `...` 占位（3 个灰色圆点动画）
- **请求超时/失败**：静默降级，不显示任何提示（不打断用户输入流）
- **限流触发**：静默跳过，不显示提示

### 2.5 移动端适配

| 策略 | 说明 |
|------|------|
| **v1 暂不支持** | 移动端 textarea 的 ghost text 覆盖层在触屏键盘弹出时坐标计算极不可靠 |
| **降级行为** | 移动端（`window.innerWidth < 768px`）自动禁用 AI 补全功能 |
| **v2 考虑** | 移动端改为底部浮层卡片展示补全建议（非内联 ghost text） |

---

## 3. 后端设计

### 3.1 API 接口定义

**端点**：`POST /api/v1/note/autocomplete`（现有端点，改造）

**请求体**（扩展 `AutocompleteRequest`）：

```python
class AutocompleteRequest(BaseModel):
    """AI 内联补全请求"""
    content: str = Field(..., max_length=2000, description="光标附近截取的内容（前端已截断）")
    cursor_position: int = Field(0, ge=0, description="光标位置（字符偏移量）")
    title: Optional[str] = Field(None, max_length=200, description="笔记标题（提供上下文）")
```

> **⚠️ I4 修复**：`content` 不再是完整笔记内容，而是**前端在光标附近截取**的上下文（前 1200 + 后 300 字符，总长 ≤ 1500）。`max_length=2000` 留有余量。这样：① 彻底避免长笔记 422 问题；② 减少带宽；③ 光标后内容也参与上下文，中部补全质量显著提升。

**响应体**：

```python
class AutocompleteResponse(BaseModel):
    """AI 内联补全响应"""
    completion: str = Field(..., description="补全文本（空串表示无建议）")
    model: str = Field("", description="使用的模型名称（前端调试用）")
    latency_ms: int = Field(0, description="推理延迟毫秒（前端性能监控）")
```

### 3.2 流式 vs 非流式选择

| 维度 | 非流式 | 流式（SSE） |
|------|--------|------------|
| 实现复杂度 | 低（标准 HTTP 请求-响应） | 高（SSE 连接管理 + 前端增量渲染） |
| 首字节延迟 | 中（等待完整生成） | 低（首个 token 即可展示） |
| 取消便利性 | 中（`AbortController` 取消 HTTP） | 低（SSE 连接需额外处理） |
| 前端集成 | 简单（`axios.post`） | 复杂（`EventSource` + 增量拼接） |
| 适用场景 | 短文本补全（< 200 字符） | 长文本生成 |

**MVP 选择：非流式**

补全目标长度为 1-200 字符，使用 qwen3.7-flash 轻量模型推理延迟约 300-800ms，用户感知可接受。非流式实现简单、取消方便（`AbortController`），MVP 阶段足够。v2 可升级为流式以获得更低首字节延迟。

### 3.3 模型选型与 Prompt 设计

**模型选型**：

| 环境 | 模型 | 说明 |
|------|------|------|
| DashScope | `qwen3.7-flash`（与本机 `.env` 的 `CLASSIFIER_MODEL` 实际值一致） | 轻量快速，推理延迟 ~300ms，成本约主模型 1/10 |
| Ollama | `qwen3:0.6b` | 本地轻量模型 |
| 降级 | 主模型（`chat_model`） | 轻量模型不可用时退化 |

> **不复用 `chat_model`（`qwen3.8-max`）**：补全为高频操作（每用户每分钟最多 10 次），使用主模型成本过高且延迟 2-5s 不可接受。

> **🟡 C4 修复（修正 I1）**：I1 让新增 `qwen3-flash` 定价条目，**方向是错的**。核对结果：
>
> | 位置 | 实际值 |
> |------|--------|
> | `factory.py:177` 代码默认值 | `qwen3-flash` |
> | `.env` 实际配置（覆盖默认值） | `CLASSIFIER_MODEL=qwen3.7-flash` ← **真正生效的** |
> | `.env.example` | `CLASSIFIER_MODEL=qwen3.7-flash-2026-07-15` |
> | `config/pricing.yaml:19` 现有条目 | `qwen3.7-flash-2026-07-15` |
>
> 代码默认值 `qwen3-flash` 在本机**从未生效**（被 `.env` 覆盖），加它等于加了个死条目。`pricing.yaml` 顶部注释明确要求"模型名须与 `ModelTrace.model` 精确一致"，而 `ModelTrace.model` 记录的是**运行时实际模型名**。
>
> **正确做法**：`AUTOCOMPLETE_MODEL` 默认值设为 `qwen3.7-flash`，并在 `pricing.yaml` 新增 **`qwen3.7-flash`** 条目（与现有 `qwen3.7-flash-2026-07-15` **并存**，两者都留）。
>
> **⚠️ 顺带发现的既有问题**：`.env` 的 `qwen3.7-flash` 与 `pricing.yaml` 的 `qwen3.7-flash-2026-07-15` 本就对不上 → **现有查询分类器 / Plan 模型的成本统计已经恒为 0**。补这个条目会一并修好它们。

**✅ 补全模型可独立配置（v1.5 补充）**：`AUTOCOMPLETE_MODEL` 是**补全专属变量**，与 `CLASSIFIER_MODEL` / `PLAN_MODEL` 完全解耦——**改 `.env` 一行即可换模型，无需改任何代码**：

```ini
# .env
AUTOCOMPLETE_ENABLED=true
AUTOCOMPLETE_MODEL=qwen3.7-flash-2026-07-15   # 想用哪个档位就写哪个
```

> ⚠️ 写法上不要留空格：`AUTOCOMPLETE_MODEL = xxx` 在 python-dotenv 下能容错（本仓库实测可读到），但 **Docker Compose 的 `env_file` 解析器不保证 trim**——容器内可能把键读成 `AUTOCOMPLETE_MODEL ` 从而退回默认值。统一写 `KEY=VALUE`。

但有 **4 条硬约束**，否则会出现"能跑但账不对"或"静默失效"：

| # | 约束 | 原因 / 处理 |
|---|------|------------|
| 1 | **`pricing.yaml` 必须有同名条目（一字不差）** | `usage_service` 按 `ModelTrace.model`（**运行时实际模型名**）查价，未命中则 `cost=0` 静默漏计。两种做法：① 直接用 pricing.yaml 已有的 `qwen3.7-flash-2026-07-15` → **零新增**；② 用新名字 → 必须同步新增同名条目 |
| 2 | **模型须接受 `extra_body={"enable_thinking": False}`** | 该参数是 qwen3 系列语义，换成其它系列可能被端点拒绝（400）。首次配置后**必须实测一次** |
| 3 | **延迟要撑得住 `request_timeout=5.0`** | 5s 是按 flash 档估的；更慢的模型会被直接掐断（前端 axios 全局 30s，见附录 G F4）。选型目标：P95 ≲ 1s、最差 ≲ 2s |
| 4 | **provider 分支必须完整（F16）** | 见下方工厂函数：`MODEL_PROVIDER` 若新增 `openai`，原示例的 `else → Ollama` 会构造出打 Ollama 地址的客户端（静默错误）。已改为显式分支 + 显式报错 |

**选型清单（补全 ≠ 分类器，关注点不同）**：

| 关注点 | 补全的要求 | 说明 |
|--------|-----------|------|
| 延迟 | **最高优先级**（停顿 600ms 后 ≤1.5s 出 ghost） | 大模型即使能关思考也偏慢 |
| 思考模式 | 必须可关闭（或本身无思考） | 见约束 2 |
| 输入 / 输出长度 | 输入 ≤1100 字符、输出 ≤200 字符 | 小模型完全够用，无需长上下文档位 |
| 价格 | 高频（≤10 次/分钟/用户），越便宜越好 | 优先 flash 档或更小档位 |
| 结论 | **不建议**用 `*-max` / thinking 档，也不建议与主模型同档 | 与分类器同档（`qwen3.7-flash`）是最稳的默认 |

**新增工厂函数** `create_autocomplete_model()`（`app/utils/factory.py`）：

```python
def create_autocomplete_model():
    """创建补全专用 Chat 模型（轻量、快速、关闭思考模式）

    环境变量：
    - AUTOCOMPLETE_ENABLED: 总开关（false → 返回 None，功能静默关闭，I8）
    - AUTOCOMPLETE_MODEL:   模型名，**可独立于 CLASSIFIER_MODEL 配置**（§3.3）；
                            须与 config/pricing.yaml 条目名一字不差，否则成本恒为 0
    """
    if os.getenv("AUTOCOMPLETE_ENABLED", "true").lower() != "true":
        logger.info("AI 补全功能已禁用（AUTOCOMPLETE_ENABLED=false）")
        return None

    provider = get_provider()
    if provider == ModelProvider.DASHSCOPE:
        model_name = os.getenv("AUTOCOMPLETE_MODEL", "qwen3.7-flash")  # ★ C4 修正
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            logger.warning("DASHSCOPE_API_KEY 未配置，补全模型退化为使用主模型")
            return create_chat_model()
        from langchain_openai import ChatOpenAI
        timeout = float(os.getenv("AUTOCOMPLETE_TIMEOUT", "5.0"))      # F9：可配
        max_tokens = int(os.getenv("AUTOCOMPLETE_MAX_TOKENS", "200"))  # F9：可配
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=DASHSCOPE_BASE_URL,
            streaming=False,          # 非流式（补全为短文本）
            http_async_client=get_shared_async_client(),
            request_timeout=timeout,  # 补全必须快速返回
            max_tokens=max_tokens,    # token 上限；字符上限由 _postprocess_completion 保证（F5）
            extra_body={"enable_thinking": False},  # 关闭思考（加速）
            callbacks=[TRACE_CALLBACK],
        )
    if provider == ModelProvider.OLLAMA:
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=os.getenv("AUTOCOMPLETE_MODEL", "qwen3:0.6b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            timeout=5.0,
            streaming=False,
            callbacks=[TRACE_CALLBACK],
        )

    # ★ F16：显式报错，禁止用 else 兜底——否则 MODEL_PROVIDER 新增取值时
    # 会静默落进 Ollama 分支，构造出打 Ollama 地址的客户端（表现为连接失败/超时）。
    # 若落地《DashScope迁移OpenAI大模型接入设计方案》，此处须补 OpenAI 分支
    # （读 OPENAI_AUTOCOMPLETE_MODEL，复用该方案的 _get_api_key / _get_base_url）。
    raise ValueError(f"create_autocomplete_model 暂不支持的 provider: {provider.value}")
```

**Prompt 设计**（`prompts/autocomplete_prompt.txt`）：

> **⚠️ N2 修复**：采用 FIM（fill-in-the-middle）式 prefix/suffix 结构，让模型同时看到光标前后的内容，明确补全衔接点在两段之间。这样前端发送的光标后 300 字符不再被丢弃，中部补全质量显著提升。

```
你是一个笔记续写助手。根据用户提供的笔记上下文，在光标位置续写接下来的内容。

规则：
1. 续写内容必须与上文风格、主题、语言保持一致
2. 续写长度控制在 1-3 句话（不超过 100 字符）
3. 直接输出续写内容，不要加任何解释、引号或前缀
4. 如果无法判断续写方向，输出空字符串

笔记标题：{title}
光标前的内容：
{context_before}
---
光标后的内容：
{context_after}
---
请在“光标前的内容”末尾续写，使其与“光标后的内容”自然衔接：
```

**上下文截取策略**（⚠️ I5 修复：删除 §3.3 的早期版本，统一使用 §3.6 版本）：

> 见 §3.6 `_truncate_context` 方法——前端发送的 `content` 已是光标附近截取的内容（前 800 + 后 300 字符，F8），后端 `_truncate_context` 进一步截取光标前 ≤ `AUTOCOMPLETE_MAX_CONTEXT`（默认 800）字符 + 光标后 ≤ 300 字符，分别作为 FIM prompt 的 `{context_before}` 和 `{context_after}`。

### 3.4 防抖与限流

**前端防抖**（详见 §4.2）：
- 600ms 停顿延迟（`setTimeout` + 取消）
- 新请求前取消上一次未完成请求（`AbortController`）

**后端限流**：

> **⚠️ S3 现状确认**：`rate_limit.py:154` 读取 `request.state.user_id`，该属性在整个代码库中从未被赋值 → 一切已认证端点的限流实际按 `anon:{客户端IP}` 计数。此判断经核对属实。
>
> **🔴 C1 修复（撤销 I11）**：v1.2 的 I11 把限流改成 router 级 `dependencies=[...]`，这会**让 S3 修复失效**。FastAPI 在 `APIRoute.__init__` 中把 router 级依赖 `insert(0, ...)` 到 `dependant.dependencies` 最前面，`solve_dependencies` 顺序求解 → **router 级依赖先于 endpoint 自身的 `Depends(get_current_user_id)` 执行**，此时 `request.state.user_id` 尚未写入。
>
> 本仓库 FastAPI 0.139.2 实测：
>
> | 写法 | `rate_limit` 读到的 `user_id` |
> |------|------|
> | `@router.post(..., dependencies=[Depends(rate_limit(...))])` | `None` ❌ |
> | `async def ep(user_id=Depends(get_current_user_id), _=Depends(rate_limit(...)))` | `'U123'` ✅ |
> | `async def ep(_=Depends(rate_limit(...)), user_id=Depends(get_current_user_id))` | `None` ❌ |
>
> 结论：**必须用参数顺序写法，且 `user_id` 参数必须排在 `rate_limit` 之前**——即 `rate_limit.py:131` docstring 里给出的原始用法。

> **🔴 C2 修复**：S3 说的"一行改动"不成立。`auth_utils.py:381-384` 现有签名为 `(authorization: Optional[str] = Header(None), db: AsyncSession = Depends(get_db))`，**没有 `request` 参数**，必须先加。

```python
# app/utils/auth_utils.py — 修复 get_current_user_id（C2：需先加 request 参数）
from fastapi import Request

async def get_current_user_id(
    request: Request,                                    # ★ C2 新增
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> str:
    # ... 现有认证逻辑不变 ...
    request.state.user_id = user_id  # ★ S3：写入 request.state，限流按 user_id 计数
    return user_id
```

```python
# note_router.py — 限流用参数顺序写法（C1：撤销 I11 的 router 级 dependencies）
@router.post("/note/autocomplete", summary="AI 内联补全")
async def autocomplete(
    data: AutocompleteRequest,
    user_id: str = Depends(get_current_user_id),          # ★ 必须在前
    _rl=Depends(rate_limit(endpoint_limit=10)),           # ★ 必须在后，才能读到 user_id
):
```

> **⚠️ 既有技术债（不在本次范围）**：项目现有 5 处限流（`chat.py` 3 处、`user.py` 3 处、`knowledge_router.py` 1 处）全部是 router 级写法，**S3 修完它们依然按 IP 计数**。v1.2 所称"一行改动，全部端点受益"不成立。这些端点要真正按 user_id 限流，需逐个改写成参数顺序写法——建议单独立项，不与本功能耦合。
>
> 注：`/auth/register`、`/auth/login`、`/auth/send-code` 三处本就应按 IP 计数（防暴力破解），无需改动。

> 限流值说明：前端防抖 600ms → 理论最大频率 ~100 次/分钟，**远高于后端 10 次/分钟**。按 C1 写法修复后按 `user_id` 计数，不再受 NAT 共享 IP 影响。
>
> **⚠️ 10 次/分钟的实际影响（v1.5 调整）**：平均 6 秒才允许一次请求，而"停顿 600ms 即触发"在正常写作中很容易超出——连续写一段话、每次停顿都算一次，一分钟内 10 次配额会被打满，之后**静默降级（429 → 无 ghost text）**，用户感知为"补全时有时无"。
>
> 建议二选一（或都做）：
> 1. **前端加最小间隔**：距上次请求 < 5s 不发（`useAutocomplete` 内加一个 `lastRequestAt` 引用即可），把频率压到限流以下，避免无谓的 429 与静默失败；
> 2. **限流值可配**：新增 `AUTOCOMPLETE_RATE_LIMIT` env（默认 10）传入 `rate_limit(endpoint_limit=...)`，成本敏感时调低、体验优先时调高。
>
> 另注：10 次/分钟是**成本控制**导向的取值（单次约 800~1000 token）。若上线后采纳率偏低，应**先排查是否被限流掐掉**，再怀疑模型质量。

**请求合并**（可选优化，v2）：
- 同一用户并发请求（多标签页）→ Redis 记录最近请求时间戳，500ms 内重复请求直接返回缓存结果
- MVP 阶段不实现，前端 `AbortController` 已足够

### 3.5 Token 用量计费集成

补全调用经 `TRACE_CALLBACK` 自动记录到 `model_trace` 表（与对话/标签生成一致），无需额外代码。关键配置：

- `set_trace_stage("autocomplete")`：标记调用阶段
- `model_trace` 记录：`user_id`、`model`、`prompt_tokens`、`completion_tokens`、`latency_ms`
- 费用通过 `model_pricing` 表的 `input_price_per_1k` / `output_price_per_1k` 自动计算

> **🔴 F17 修复（实施阶段实测发现，v1.5 补录）**：上面"无需额外代码"的结论**不成立**。
> `set_trace_context()` 目前只在 `chat.py` / `chat_service.py` / `memory_compressor.py` 调用，**note 类端点从不设置上下文**；而 `set_trace_stage()` 在无上下文时是**空操作**（`model_trace.py:55-57`）。后果：
> - `model_traces.user_id` 落为**空串**、`stage` 落为默认 `chat`；
> - `GET /usage/summary` 按 `user_id` 过滤 → **补全调用在按用户聚合的用量/费用中完全不可见**。
>
> **修复**：在 router 内先建上下文再调模型（`note_router.py`）：
> ```python
> set_trace_context(new_request_id(), user_id=user_id, stage="autocomplete")
> try:
>     result = await _get_note_service().ai_autocomplete(...)
> finally:
>     clear_trace_context()
> ```
> **实测验证**（2026-09-09，真实 HTTP）：`by_stage={"stage":"autocomplete","calls":1,...}`、`by_model={"model":"qwen3.8-flash","calls":1,"cost_cny":0.0001}`。
>
> ⚠️ 同一缺陷也影响**其它 note 端点的后台 LLM 调用**（如自动标签）——它们同样没有 trace 上下文，成本归不到用户名下。本方案只修补全；其余建议单独立项。


> **⚠️ I1 → C4 修正**：需确保 `config/pricing.yaml` 包含 **`qwen3.7-flash`** 定价条目（**不是** `qwen3-flash`——后者被 `.env` 覆盖、在本机从未生效），否则费用统计恒为 0。

### 3.6 后端 Service 方法

**文件**：`app/services/note_service.py` — 新增方法

```python
async def ai_autocomplete(
    self, content: str, cursor_position: int, title: str = ""
) -> dict:
    """
    AI 内联补全

    流程：
    1. 截取光标前 ≤ 800 + 光标后 ≤ 300 字符（N2：FIM 式 prefix/suffix）
    2. 加载 autocomplete prompt 并填充
    3. 调用轻量 LLM 生成补全
    4. 后处理：去除引号/前缀/多余空白，截断至 200 字符

    Args:
        content: 前端已截取的光标附近内容（前 1200 + 后 300 字符，I4）
        cursor_position: 光标位置（在截取内容中的偏移量）
        title: 笔记标题（提供上下文）

    Returns:
        {"completion": str, "model": str, "latency_ms": int}
    """
    import time
    from app.utils.prompt_loader import format_prompt
    from app.core.model_trace import set_trace_stage

    if not self.autocomplete_model:
        return {"completion": "", "model": "", "latency_ms": 0}

    # 1. 截取上下文（N2：分别截取光标前后）
    context_before, context_after = self._truncate_context(content, cursor_position)
    if len(context_before.strip()) < 10:
        return {"completion": "", "model": "", "latency_ms": 0}

    # 2. 构建 prompt（N2：FIM 式 prefix/suffix）
    # ★ C7 修复：format_prompt 会抛 FileNotFoundError（模板文件缺失）/ KeyError
    #    （prompt.yaml 未配置）。原设计放在 try 外 → 直接 500，而非静默降级。
    try:
        prompt = format_prompt(
            "autocomplete",
            title=title or "无标题",
            context_before=context_before,
            context_after=context_after or "（无）",
        )
    except (FileNotFoundError, KeyError) as e:
        logger.error(f"补全 prompt 模板加载失败: {e}")
        return {"completion": "", "model": "", "latency_ms": 0}

    # 3. 调用 LLM
    set_trace_stage("autocomplete")
    start = time.monotonic()
    try:
        response = await self.autocomplete_model.ainvoke(
            [("system", "你是一个笔记续写助手，直接输出续写内容。"),
             ("user", prompt)]
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        raw_text = response.content or ""
    except Exception as e:
        logger.warning(f"AI 补全调用失败: {e}")
        return {"completion": "", "model": "", "latency_ms": 0}

    # 4. 后处理
    completion = self._postprocess_completion(raw_text)
    model_name = getattr(self.autocomplete_model, 'model_name', '')

    return {
        "completion": completion,
        "model": model_name,
        "latency_ms": latency_ms,
    }

def _truncate_context(self, content: str, cursor_position: int,
                      max_before: int = 800, max_after: int = 300) -> tuple[str, str]:
    """
    截取光标前后上下文（N2：返回 (before, after) 用于 FIM prompt）

    Returns:
        (context_before, context_after)
        - context_before: 光标前 ≤ max_before 字符，优先段落边界截断
        - context_after:  光标后 ≤ max_after 字符
    """
    # 光标前：截取 + 段落边界优先
    before = content[:cursor_position]
    if len(before) > max_before:
        truncated = before[-max_before:]
        para_start = truncated.rfind("\n\n")
        if para_start > max_before // 2:  # 至少保留一半内容
            before = truncated[para_start + 2:]
        else:
            before = truncated

    # 光标后：直接截取 max_after 字符
    after = content[cursor_position:cursor_position + max_after]

    return before, after

def _postprocess_completion(self, text: str, max_len: int = 200) -> str:
    """
    后处理模型输出：
    - 去除成对的首尾引号（模型常加 "" 或 ''）
    - 去除换行后的解释性文本（如 "续写：..."）
    - 截断至 max_len

    ⚠️ C3：引号集合必须用常量而非行内元组字面量，见下方说明
    """
    text = text.strip()
    # 去除成对首尾引号（★ C3 修复）
    if len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        text = text[1:-1].strip()
    # 只取第一行（避免模型输出多段解释）
    text = text.split("\n")[0].strip()
    # 截断（CJK 无空格，rsplit 找不到空格时返回原串，不会误截）
    if len(text) > max_len:
        head = text[:max_len]
        text = head.rsplit(" ", 1)[0] if " " in head else head
    return text
```

> **🔴 C3 修复（严重：静默逻辑错误，非语法错误）**
>
> v1.2 的原写法把中英文引号混在一个行内元组里：
>
> ```python
> if text and text[0] in ('"', "'", '"', ''') and text[-1] in ('"', "'", '"', '''):
> ```
>
> 第 4 个元素 `'''` 会被 Python 解析为**三引号字符串的起始**，把后面的 `) and text[-1] in ('"', "'", '"', ` 整段吞进字符串字面量。AST 实测结果：
>
> ```
> BoolOp(op=And, values=[Name('text'), Compare(...)])      ← 只剩 2 个分支
> comparators=[Tuple(elts=['"', "'", '"', ') and text[-1] in (\'"\', "\'", \'"\', '])]
> ```
>
> **`text[-1]` 的检查完全消失了。** 危害在于它**能通过语法检查**，也**能通过 §6.2 现有测试用例**（`"abc"` 首尾都是引号，结果碰巧正确），但对 `"abc`（模型只输出前引号，很常见）会把最后一个正常字符 `c` 切掉。
>
> **修复**：引号集合提为模块级常量，杜绝行内引号嵌套：
>
> ```python
> # app/services/note_service.py 模块级
> _QUOTE_CHARS = frozenset('"\'' + '“”‘’')  # 半角 + 中文弯引号
> ```
>
> 同时 §6.2 需新增用例：**只有前引号**（`"abc` → `abc`）、**只有后引号**（`abc"` → `abc`）、**单字符**（`"` → 不越界）。

### 3.7 Router 改造

```python
@router.post("/note/autocomplete", summary="AI 内联补全")
async def autocomplete(
    data: AutocompleteRequest,
    user_id: str = Depends(get_current_user_id),      # ★ C1：必须在 rate_limit 之前
    _rl=Depends(rate_limit(endpoint_limit=10)),       # ★ C1：撤销 I11 的 router 级写法
):
    """AI 内联补全（根据上下文续写）"""
    result = await _get_note_service().ai_autocomplete(
        content=data.content,
        cursor_position=data.cursor_position,
        title=data.title or "",
    )
    return success_response(data=result)
```

---

## 4. 前端设计

> **✅ 实施现状（2026-09-09）：方案 A（光标处内联 ghost text）已落地；底部建议条已移除。**
>
> 起因：产品反馈"底部建议条容易被忽略"。改为**光标处内联灰色 ghost text**（§4.3 方案 A）：
> - `front/src/components/GhostTextArea.tsx`（新）：透明 textarea + highlighter 覆盖层；ghost 在底层**撑开后续文字**（不遮挡正文）；IME 组合期切回「textarea 可见 + highlighter 隐藏」；`enabled=false` 退回原生 textarea（§4.3.5 降级）；
> - `front/src/styles/ghost-textarea.css`（新）：两层共用 `.gta-typography`，等宽字体由 `.gta-mono` 修饰类控制（F1）；选区半透明；`scrollbar-gutter: stable`；尾行补位；
> - `front/src/pages/NoteEditor.tsx`：两处 textarea 换成 `<GhostTextArea>`（txt 传 `enabled={false}`）；底部建议条与 `note.autocomplete.*` 词条已删除；
> - `useAutocomplete`：**请求发起时即记录插入点**，loading 的 `…` 与最终建议都落在光标处。
>
> **门禁全绿**：`npm run lint` / `tsc --noEmit` / `npm test`（**10 文件 77 条**：`GhostTextArea.test.tsx` 10 条 + `useAutocomplete.test.tsx` 7 条 + 原 60 条）/ `npm run build`。
>
> **⚠️ 仍待办**：§4.3.3 的 IME 真机验证（附录 H）。代码已实现切换逻辑，但中文输入法下的实际表现必须真机确认；若 H.2 第 1/2/3 项失败，按 H.3 回退 B-限定，或恢复建议条（改动集中在 `NoteEditor` 的 UI 部分）。

### 4.1 整体架构

```
NoteEditor.tsx
    │
    ├─ 现有：标题 / 标签 / 分类 / 工具栏 / 预览
    │
    ├─ useAutocomplete hook（防抖 + 请求 + 状态管理）
    │
    └─ 新增：<GhostTextArea>  ← 封装「透明 textarea + highlighter div」两层
        │
        ├─ ① highlighter div（absolute inset-0, pointer-events:none, aria-hidden）
        │     渲染：{before}<span class="ghost">{completion}</span>{after}
        │
        ├─ ② textarea（absolute inset-0, color:transparent, caret 可见）
        │     承接：onChange / onKeyDown / onScroll / onSelect / composition
        │
        └─ 共享样式类 .gta-typography（两层逐像素一致，见 §4.3）
```

> **组件化的理由**：txt 模式与 md 模式各有一个 textarea（`NoteEditor.tsx:310` / `:330`），v1.2 的 I2 靠 `renderGhostOverlay()` 函数复用。方案 A 的两层结构 + 6 个事件处理器再用裸函数复用会很脏，**封装为受控组件 `<GhostTextArea>`**，两处各用一次即可，同时天然解决 I2（双 textarea 覆盖）与 C6（定位原点）。

### 4.2 `useAutocomplete` Hook

```typescript
// front/src/hooks/useAutocomplete.ts

interface AutocompleteState {
  /** 补全建议文本（空 = 无建议） */
  completion: string;
  /** 是否正在请求 */
  isLoading: boolean;
  /** 截断内容内的光标偏移（仅用于 API payload，I4） */
  cursorPosition: number;
  /** 完整内容内的光标偏移（v1.4：作为 GhostTextArea 的 ghostAt 切分点 + Tab 插入位置，N1 修复） */
  fullCursorPosition: number;
}

interface UseAutocompleteOptions {
  /** 停顿延迟（ms），默认 600 */
  debounceMs?: number;
  /** 是否启用，默认 true */
  enabled?: boolean;
}

export function useAutocomplete(options?: UseAutocompleteOptions) {
  const { debounceMs = 600, enabled = true } = options ?? {};

  const [state, setState] = useState<AutocompleteState>({
    completion: '',
    isLoading: false,
    cursorPosition: 0,
    fullCursorPosition: 0,
  });

  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 用户输入变化时调用（由 NoteEditor 的 onChange 触发） */
  const onContentChange = useCallback((
    content: string,
    cursorPosition: number,       // 截断内容内的偏移（API payload 用）
    fullCursorPosition: number,   // 完整内容内的偏移（ghost text / Tab 插入用，N1）
    title: string,
  ) => {
    // 1. 清除上一次计时器
    if (timerRef.current) clearTimeout(timerRef.current);
    // 2. 取消上一次未完成请求
    if (abortRef.current) abortRef.current.abort();
    // 3. 清除当前 ghost text
    setState({ completion: '', isLoading: false, cursorPosition: 0, fullCursorPosition: 0 });

    // 4. 检查触发条件（⚠️ I6 修复：检查光标前字符数，而非总内容长度）
    if (!enabled || fullCursorPosition < 10) return;

    // 5. 启动防抖计时器
    timerRef.current = setTimeout(async () => {
      const controller = new AbortController();
      abortRef.current = controller;
      // ★ 低优修复：发起请求时写入 fullCursorPosition，loading 指示可渲染
      setState(prev => ({ ...prev, isLoading: true, fullCursorPosition }));

      try {
        const res = await notesApi.autocomplete(
          { content, cursor_position: cursorPosition, title },
          { signal: controller.signal },
        );
        const data = res.data.data;
        if (data.completion) {
          setState({
            completion: data.completion,
            isLoading: false,
            cursorPosition: cursorPosition,
            fullCursorPosition: fullCursorPosition,
          });
        } else {
          setState({ completion: '', isLoading: false, cursorPosition: 0, fullCursorPosition: 0 });
        }
      } catch (err) {
        if (!axios.isCancel(err)) {
          setState({ completion: '', isLoading: false, cursorPosition: 0, fullCursorPosition: 0 });
        }
      }
    }, debounceMs);
  }, [debounceMs, enabled]);

  /** 采纳补全（Tab 键触发）——返回文本 + 完整内容光标位置（S2/N1 修复） */
  const accept = useCallback(() => {
    const text = state.completion;
    const pos = state.fullCursorPosition;  // ★ N1：使用完整内容偏移
    setState({ completion: '', isLoading: false, cursorPosition: 0, fullCursorPosition: 0 });
    return { text, cursorPosition: pos };
  }, [state.completion, state.fullCursorPosition]);

  /** 丢弃补全（Esc / 继续输入 / 光标移动 / 滚动触发） */
  const dismiss = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (abortRef.current) abortRef.current.abort();
    setState({ completion: '', isLoading: false, cursorPosition: 0, fullCursorPosition: 0 });
  }, []);

  // 组件卸载时清理
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  return { ...state, onContentChange, accept, dismiss };
}
```

### 4.3 Ghost Text 渲染（方案 A：覆盖层）

> **⚠️ v1.4 变更**：本节完全替换 v1.3 的「测量 div + 绝对定位」实现。
> **`front/src/utils/cursorPosition.ts` 不再需要**——方案 A 无需任何像素测量，v1.2 的 I7（Firefox `computedStyle.font` 兼容）与 v1.3 的 C6（定位原点错位）**随之全部消失**。

#### 4.3.1 共享样式（两层逐像素一致）

排版属性必须由**同一个 class** 提供，绝不能一层写 Tailwind、另一层写内联样式——这是覆盖层方案最常见的漂移来源。

```css
/* front/src/styles/ghost-textarea.css（或写进 index.css） */

.gta-typography {
  /* 与 NoteEditor 现有 textarea 的 p-4 text-sm leading-relaxed font-mono 对齐 */
  padding: 1rem;                      /* = p-4   */
  font-size: 0.875rem;                /* = text-sm */
  line-height: 1.625;                 /* = leading-relaxed */
  /* ★ F1：此处不设 font-family —— 等宽由 .gta-mono 修饰类提供（见下） */
  letter-spacing: normal;
  tab-size: 4;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  word-break: normal;
  border: 0;
  box-sizing: border-box;
  scrollbar-gutter: stable;           /* ★ 关键：两层滚动条占位一致，否则换行漂移 */
}

/* ★ F1：等宽字体必须由独立修饰类提供，绝不能写进 .gta-typography——
   否则 txt 模式（mono=false）也会变等宽，与改造前 NoteEditor.tsx:315
   继承 --font-body 的表现不一致（静默的视觉回归）。md 模式两层同时加 .gta-mono。 */
.gta-mono { font-family: var(--font-mono); }

.gta-textarea {
  color: transparent;
  caret-color: var(--color-text);
  -webkit-text-fill-color: transparent;   /* Safari */
  background: transparent;
  resize: none;
  outline: none;
  position: absolute;
  inset: 0;
  overflow-y: auto;
}
/* ★ 关键：上层选区必须半透明，否则盖住下层文字 */
.gta-textarea::selection { background: rgba(59, 130, 246, 0.3); }

.gta-highlighter {
  position: absolute;
  inset: 0;
  overflow: hidden;                   /* 滚动由 textarea 驱动，见 onScroll */
  pointer-events: none;
  user-select: none;
  color: var(--color-text);
}

.gta-ghost { color: var(--color-text-tertiary); opacity: 0.5; }

/* IME 组合期间：textarea 恢复可见，highlighter 隐藏（见 §4.3.3） */
.gta-composing .gta-textarea {
  color: var(--color-text);
  -webkit-text-fill-color: var(--color-text);
}
.gta-composing .gta-highlighter { visibility: hidden; }
```

#### 4.3.2 `<GhostTextArea>` 组件

```tsx
// front/src/components/GhostTextArea.tsx

interface GhostTextAreaProps {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  /** ghost text 内容，空串 = 不显示 */
  completion: string;
  /** 请求中（在光标处显示闪烁 …） */
  isLoading: boolean;
  /** completion 对应的完整内容光标偏移（N1 的 fullCursorPosition） */
  ghostAt: number;
  /** 是否等宽字体（md 模式 true，txt 模式 false）；控制 `.gta-mono` 修饰类（F1） */
  mono?: boolean;
  /** 是否启用双层渲染；false → 退回原生 textarea（§4.3.5 降级开关，F2） */
  enabled?: boolean;
  placeholder?: string;
  className?: string;
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  onInteract?: () => void;       // 点击/失焦/方向键 → dismiss
  textareaRef: React.RefObject<HTMLTextAreaElement>;
}

export function GhostTextArea({
  value, onChange, completion, isLoading, ghostAt,
  mono = true, enabled = true, placeholder, className, onKeyDown, onInteract, textareaRef,
}: GhostTextAreaProps) {
  const highlighterRef = useRef<HTMLDivElement>(null);
  const [isComposing, setIsComposing] = useState(false);

  // ★ 滚动同步：textarea 是滚动驱动方，highlighter 被动跟随
  const handleScroll = useCallback(() => {
    const ta = textareaRef.current, hl = highlighterRef.current;
    if (!ta || !hl) return;
    hl.scrollTop = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
  }, [textareaRef]);

  // ★ F2：降级为原生 textarea（关闭开关 / 移动端 / txt 模式，§4.3.5）
  // ⚠️ 必须放在全部 Hook 调用之后：条件早退会让 Hook 调用数随 enabled 变化，
  //    违反 Rules of Hooks（本项目 eslint 中 react-hooks/rules-of-hooks 为 error 级）。
  // 降级路径必须复刻改造前两处 textarea 的样式差异：
  //   txt（NoteEditor.tsx:315）不带等宽字体；md（:335）带 font-[var(--font-mono)]。
  // 否则"降级成功"却把字体改了，等于换个坑踩。
  if (!enabled) {
    return (
      <textarea
        ref={textareaRef} value={value} onChange={onChange}
        onKeyDown={onKeyDown} onBlur={onInteract}
        placeholder={placeholder} spellCheck={false}
        className={`${className ?? ''} w-full min-h-0 resize-none p-4 text-sm leading-relaxed text-[var(--color-text)] bg-transparent outline-none ${
          mono ? 'font-[var(--font-mono)]' : ''
        }`}
      />
    );
  }

  // ghost 可见时才切分文本，避免每次输入都做两次 slice
  const showGhost = !isComposing && (completion.length > 0 || isLoading);
  const before = showGhost ? value.slice(0, ghostAt) : value;
  const after  = showGhost ? value.slice(ghostAt) : '';

  return (
    <div className={`relative ${className ?? ''} ${isComposing ? 'gta-composing' : ''}`}>
      {/* ① 底层：负责显示 */}
      <div
        ref={highlighterRef}
        aria-hidden="true"
        className={`gta-typography gta-highlighter ${mono ? 'gta-mono' : ''}`}
      >
        {before}
        {/* ⚠️ 必须用 showGhost 门控（F18）：否则 IME 组合期（highlighter 被隐藏）
            仍会渲染 ghost span，且因未切分而落在文末——视觉被掩盖、语义错误 */}
        {showGhost && completion && <span className="gta-ghost">{completion}</span>}
        {showGhost && isLoading && !completion && <span className="gta-ghost animate-pulse">…</span>}
        {after}
        {/* ★ 尾行塌陷修复：div 会吞掉末尾换行，textarea 不会 */}
        {'\n'}
      </div>

      {/* ② 上层：负责交互 */}
      <textarea
        ref={textareaRef}
        value={value}
        onChange={onChange}
        onKeyDown={onKeyDown}
        onScroll={handleScroll}
        onClick={onInteract}
        onBlur={onInteract}
        onKeyUp={(e) => {
          if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown']
              .includes(e.key)) onInteract?.();
        }}
        onCompositionStart={() => { setIsComposing(true); onInteract?.(); }}
        onCompositionEnd={() => setIsComposing(false)}
        placeholder={placeholder}
        spellCheck={false}
        className={`gta-typography gta-textarea ${mono ? 'gta-mono' : ''}`}
      />
    </div>
  );
}
```

#### 4.3.3 🔴 IME 预编辑串透明问题（头号风险）

`color: transparent` 不只作用于已提交的文本，**拼音候选阶段的预编辑串（preedit，带下划线的那段）同样会变透明**——中文用户敲拼音时屏幕上什么都看不到，功能直接不可用。这是覆盖层方案在 CJK 场景下最容易翻车的地方，v1.3 的 §5.7「防抖机制天然兼容 IME」结论**在方案 A 下不再成立**。

处理方式（上方组件已实现）：

| 时机 | 动作 |
|------|------|
| `compositionstart` | 加 `.gta-composing` → textarea 恢复不透明 + highlighter `visibility: hidden`；同时 `dismiss()` 清掉 ghost |
| 组合期间 | 用户看到的是 **textarea 自身**渲染的预编辑串，与原生体验完全一致 |
| `compositionend` | 移除 `.gta-composing` → 切回透明层；随后 `onChange` 触发防抖，600ms 后重新请求 |

> **必须真机验证**：微软拼音、搜狗输入法、macOS 原生拼音在 Chrome / Edge / Safari 下各测一遍。`compositionend` 与 `onChange` 的触发顺序在各浏览器间存在差异，需确认切回透明时正文已同步。

#### 4.3.4 其余边界

| 边界 | 处理 |
|------|------|
| **滚动条宽度漂移** | 两层统一 `scrollbar-gutter: stable`。否则 textarea 出滚动条时内容区变窄、换行位置比 highlighter 早 → 整体错位 |
| **尾行塌陷** | highlighter 内容末尾补 `{'\n'}`（组件已实现） |
| **ghost 撑高导致两层 scrollHeight 不等** | highlighter 恒 `overflow: hidden` 且 scrollTop 由 textarea 驱动；ghost 超出可视区的部分被裁剪，可接受（用户视线在光标处） |
| **选区可见性** | `.gta-textarea::selection` 半透明蓝，底层文字透出 |
| **txt / md 字体不同** | `mono` prop 控制 `.gta-mono` 修饰类：md 传 `true`，txt 传 `false`，两层同时加/不加。**`font-family` 不得写进 `.gta-typography`**（F1：否则 txt 模式被静默改成等宽，与 `NoteEditor.tsx:315` 现状不符） |
| **原生 undo 栈** | 受控组件 + `setContent` 本就会破坏原生 undo，与现有工具栏 `wrapSelection` / `prefixLine`（`NoteEditor.tsx:117-146`）行为一致，**非新增回归** |
| **性能** | 每次按键 highlighter 重渲染完整正文。md 模式右栏的 `ReactMarkdown` 重渲染开销远大于此，无新增瓶颈 |

#### 4.3.5 🟠 方案 A 的爆炸半径：必须保留降级到裸 textarea 的开关

方案 A 与方案 B 有一个**性质上的差别**，v1.3 的 C8 对比表未涵盖：

> **方案 B** 只在 ghost text 显示时叠加一个 span——AI 功能关闭 / 失败 / 限流时，编辑器与现在**完全一致**，零风险。
> **方案 A** 把 textarea 永久改成双层结构——**即使一次补全都没触发**，用户日常打字看到的也是 highlighter 层渲染的文字。任何排版漂移（字体回退、滚动条、`letter-spacing`、浏览器差异）都会**直接损伤最核心的"写笔记"体验**，且与 AI 无关。

这是采用方案 A 必须付出的代价，**必须配套一个降级开关**：

> **实现位置（F2）**：降级分支已并入 §4.3.2 的组件实现（`enabled=false` 时早退），降级路径**复刻改造前两处 textarea 的样式差异**——txt 不带等宽字体（对应 `NoteEditor.tsx:315`）、md 带（`:335`），避免"降级了但字体变了"。

`enabled` 取值与补全功能一致（三者任一为假即降级），**已在 §4.4 接线为 `enabled={!isTxt && !isMobile}`（txt 实例恒传 `false`）**：

| 条件 | 说明 |
|------|------|
| `AUTOCOMPLETE_ENABLED=false`（后端返回空 → 前端可由用户设置开关同步） | 功能总开关关闭时，编辑器行为完全回到现状 |
| `window.innerWidth < 768`（§2.5 移动端） | 移动端触屏 + 虚拟键盘下双层方案风险最高，直接降级 |
| `isTxt`（MVP 阶段 txt 模式不启用） | 与 `useAutocomplete({ enabled: !isTxt && !isMobile })` 保持一致（txt 实例恒传 `enabled={false}`） |

> **联调验收必须包含**：关闭 `AUTOCOMPLETE_ENABLED` 后，md / txt 两种模式的编辑体验与改造前**逐像素一致**（可用截图 diff 验证）。这条通过了，方案 A 的风险才算被兜住。

### 4.4 与 `NoteEditor.tsx` 集成

> **⚠️ v1.4 变更**：改用 `<GhostTextArea>` 组件替换两处裸 `<textarea>`。v1.2 的 `renderGhostOverlay()` 共享函数、`ghostPos` state、坐标计算 `useEffect` **全部删除**（I2 与 C6 由组件封装天然解决）。

```tsx
// NoteEditor.tsx 改造要点（仅展示新增/修改部分）

import { useAutocomplete } from '../hooks/useAutocomplete';
import { GhostTextArea } from '../components/GhostTextArea';   // ★ v1.4：替代 cursorPosition.ts

export default function NoteEditor({ ... }) {
  // ... 现有状态（content / title / editorMode / textareaRef 等保持不变）...

  // ★ F2：移动端与 txt 模式都降级为原生 textarea（§2.5 / §4.3.5）
  const isMobile = typeof window !== 'undefined' && window.innerWidth < 768;

  const autocomplete = useAutocomplete({
    debounceMs: 600,
    enabled: !isTxt && !isMobile,   // MVP 仅桌面 md 模式启用（txt / 移动端第二阶段）
  });

  // ★ I2：editorMode 切到 preview 时 textarea 卸载，需 dismiss
  useEffect(() => {
    if (editorMode === 'preview') autocomplete.dismiss();
  }, [editorMode]);

  // 切换笔记时清理残留建议（组件已 key 重挂载，此处为额外保险）
  useEffect(() => { autocomplete.dismiss(); }, [note?.id]);

  // ★ I4/N1：前端在光标附近截取上下文（前 1200 + 后 300）
  const truncateAroundCursor = useCallback((fullContent: string, cursorPos: number) => {
    const before = fullContent.slice(Math.max(0, cursorPos - 1200), cursorPos);
    const after  = fullContent.slice(cursorPos, cursorPos + 300);
    return {
      content: before + after,
      cursorPosition: before.length,   // 截断内容内的偏移（API payload 用）
      fullCursorPosition: cursorPos,   // 完整内容内的偏移（ghost 渲染 / Tab 插入用，N1）
    };
  }, []);

  const handleContentChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const newContent = e.target.value;
    setContent(newContent);

    // ★ 5.4：有选区时不触发补全
    if (e.target.selectionStart !== e.target.selectionEnd) {
      autocomplete.dismiss();
      return;
    }
    const { content: truncated, cursorPosition, fullCursorPosition } =
      truncateAroundCursor(newContent, e.target.selectionStart);
    autocomplete.onContentChange(truncated, cursorPosition, fullCursorPosition, title);
  }, [autocomplete, title, truncateAroundCursor]);

  // ★ S2：Tab 采纳时在光标处插入（而非追加到末尾）
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // ★ v1.4：IME 组合期间不拦截任何按键（Enter/Esc 是选词/取消候选）
    if (e.nativeEvent.isComposing) return;

    if (e.key === 'Tab' && autocomplete.completion) {
      e.preventDefault();
      const { text, cursorPosition: pos } = autocomplete.accept();   // N1：完整内容偏移
      setContent(prev => prev.slice(0, pos) + text + prev.slice(pos));
      requestAnimationFrame(() => {
        const ta = textareaRef.current;
        if (!ta) return;
        ta.focus();
        const newPos = pos + text.length;
        ta.setSelectionRange(newPos, newPos);
      });
    } else if (e.key === 'Escape' && autocomplete.completion) {
      e.preventDefault();
      autocomplete.dismiss();
    } else if (e.key !== 'Tab') {
      // 继续输入 → 丢弃 ghost text（Tab 无补全时放行，保持原生焦点切换）
      autocomplete.dismiss();
    }
  }, [autocomplete]);

  // ★ I3：光标移动 / 点击 / 失焦时丢弃陈旧补全（滚动不再 dismiss，v1.4 改为同步）
  const handleDismissOnInteract = useCallback(() => {
    if (autocomplete.completion || autocomplete.isLoading) autocomplete.dismiss();
  }, [autocomplete.completion, autocomplete.isLoading]);

  return (
    <div className="flex flex-col h-full px-4 py-3 overflow-hidden">
      {/* ... 现有顶部栏 / 标签分类栏 / 工具栏 ... */}

      {isTxt ? (
        /* 纯文本模式：单栏 */
        <GhostTextArea
          textareaRef={textareaRef}
          value={content}
          onChange={handleContentChange}
          onKeyDown={handleKeyDown}
          onInteract={handleDismissOnInteract}
          completion={autocomplete.completion}
          isLoading={autocomplete.isLoading}
          ghostAt={autocomplete.fullCursorPosition}
          enabled={false}
          mono={false}
          placeholder={t('note.content')}
          className="flex-1 w-full min-h-0"
        />
      ) : (
        <div className="flex flex-1 min-h-0">
          {/* 左栏：Markdown 源码编辑 */}
          {editorMode !== 'preview' && (
            <div className={`flex flex-col transition-all duration-300 ${
              editorMode === 'edit' ? 'w-full' : 'w-1/2 border-r border-[var(--color-border)]'
            }`}>
              {editorMode === 'edit' && (
                <div className="px-3 py-1.5 text-xs ... shrink-0">编辑</div>
              )}
              <GhostTextArea
                textareaRef={textareaRef}
                value={content}
                onChange={handleContentChange}
                onKeyDown={handleKeyDown}
                onInteract={handleDismissOnInteract}
                completion={autocomplete.completion}
                isLoading={autocomplete.isLoading}
                ghostAt={autocomplete.fullCursorPosition}
                enabled={!isMobile}
                mono
                placeholder={t('note.content')}
                className="flex-1 w-full min-h-0"
              />
            </div>
          )}

          {/* 右栏：实时渲染预览（现有逻辑不变） */}
          {editorMode !== 'edit' && ( /* ... */ )}
        </div>
      )}
    </div>
  );
}
```

**与 v1.2 版本的差异对照**：

| v1.2（方案 B） | v1.4（方案 A） |
|---|---|
| `ghostPos` state + 坐标计算 `useEffect` | ❌ 删除——无需像素测量 |
| `renderGhostOverlay()` 共享函数 | ❌ 删除——由 `<GhostTextArea>` 组件封装 |
| 两处裸 `<textarea>` + 6 个内联 handler | ✅ 两处 `<GhostTextArea>`，handler 收敛进组件 |
| `onScroll` → `dismiss()` | ✅ `onScroll` → 同步 highlighter 滚动位置 |
| 外层需加 `relative` wrapper（C6） | ✅ 组件自带 `relative`，坐标系天然对齐 |
| 无 IME 特殊处理 | ✅ `composition` 事件切换渲染模式（§4.3.3） |
| `handleKeyDown` 对所有键 `dismiss()` | ✅ 补 `isComposing` 早退 + Tab 无补全时放行 |

> **⚠️ `className` 的约束**：传给 `<GhostTextArea>` 的 `className` 只能含**布局**类（`flex-1` / `w-full` / `min-h-0`），**不得**含 `p-*` / `text-*` / `leading-*` / `font-*`——排版属性必须由 `.gta-typography` 统一提供，否则两层漂移（§4.3.1）。这是本组件唯一容易被后续维护者踩的坑，建议在组件源码顶部注释里写死这条约定。

### 4.5 前端 API 层改造

**文件**：`front/src/api/notes.ts`

```typescript
// 现有 autocomplete 方法扩展（增加 title + AbortSignal 支持）
autocomplete: (
  data: { content: string; cursor_position?: number; title?: string },
  config?: { signal?: AbortSignal },  // 新增：支持取消
) =>
  client.post<ApiResponse<{ completion: string; model: string; latency_ms: number }>>(
    endpoints.note.autocomplete, data, config
  ),
```

**文件**：`front/src/types/api.ts` — 新增类型

```typescript
/** AI 内联补全请求 */
export interface AutocompleteRequest {
  content: string;
  cursor_position?: number;
  title?: string;
}

/** AI 内联补全响应 */
export interface AutocompleteResponse {
  completion: string;
  model: string;
  latency_ms: number;
}
```

---

## 5. 边界情况

### 5.1 网络延迟 / 超时处理

| 场景 | 处理 |
|------|------|
| 请求超时（> 5s） | 后端 `request_timeout=5.0` 自动抛出异常 → 返回空 completion → 前端静默跳过 |
| 网络断开 | 前端 `catch` 捕获异常 → 清除 ghost text → 不显示错误提示 |
| 请求被取消（`AbortError`） | 前端 `axios.isCancel(err)` 判断 → 静默忽略。**⚠️ I12**：客户端取消后，服务端 DashScope 调用照常执行并计费（FastAPI 无法联动取消 upstream LLM 请求），属已知成本 |
| 后端 429 限流 | 前端收到 429 → 静默跳过（不弹 toast） |
| **隐私 / 提示注入（F12）** | 补全会把**光标附近的笔记片段**发往 LLM（与对话、自动标签等一致）；**补全输出仅作为纯文本插入，不执行其中任何指令**；如需完全离线可切 `MODEL_PROVIDER=ollama` |

### 5.2 模型返回空内容

- 后端 `_postprocess_completion` 返回空串 → 前端收到 `completion: ""` → 不渲染 ghost text
- 不视为错误，不记录 error 日志（正常业务场景：模型判断无法续写）

### 5.3 编辑器切换笔记时清理残留建议

- `NoteEditor` 以 `key={note?.id ?? 'new'}` 挂载（现有设计），切换笔记时组件**完全重挂载**
- `useAutocomplete` hook 的 `useEffect` cleanup 自动取消未完成请求 + 清除计时器
- ghost text 状态随组件销毁自然清除

> **额外保险**：`useEffect(() => { autocomplete.dismiss() }, [note?.id])` 在 note 变化时显式清理。

### 5.4 多光标 / 选区场景

| 场景 | 处理 |
|------|------|
| 有文本选中（`selectionStart !== selectionEnd`） | 不触发补全（选区意味着用户要替换/操作选中内容） |
| 光标在标题输入框 | 不触发补全（补全仅绑定 textarea 的 `onChange`） |
| 光标在文档开头（`cursor_position === 0`） | 不触发（`fullCursorPosition < 10` 条件不满足，I6 修复） |

### 5.5 纯文本模式（txt format）兼容性

| 策略 | 说明 |
|------|------|
| **MVP** | txt 模式**不启用** AI 补全（`enabled: !isTxt`） |
| **原因** | txt 模式 textarea 无 Markdown 语法感知，补全内容可能与纯文本风格不协调 |
| **v1.1** | 扩展支持 txt 模式（调整 prompt 适配纯文本风格） |

### 5.6 与现有自动标签生成的 LLM 调用竞争

| 维度 | 分析 |
|------|------|
| **模型实例** | 补全使用独立的 `autocomplete_model`（qwen3.7-flash），自动标签使用 `chat_model`（qwen3.8-max）→ **无竞争** |
| **HTTP 连接池** | 共享 `get_shared_async_client()`（100 max connections）→ 高并发下可能排队，但补全为低频操作（10 次/分钟），影响可忽略 |
| **后台任务** | 补全为同步请求-响应，不走 `spawn_background_task` → 与 `_auto_tag_and_review` 后台任务无资源竞争 |
| **Token 预算** | 补全 token 计入 `model_trace`，与对话/标签共享用户级预算（如有预算限制） |

### 5.7 输入法（IME）兼容

> **⚠️ v1.4 重写**：v1.3 的结论「无需特殊处理，防抖机制天然兼容 IME」**在方案 A 下不再成立**。透明 textarea 会连带隐藏 IME 预编辑串，必须显式处理。

| 场景 | 处理 |
|------|------|
| 中文输入法组合中（防抖层面） | `onChange` 在 IME 组合期间持续触发 → 每次重置防抖计时器 → 仅在选词完成后 600ms 才真正发请求。**这部分结论仍成立** |
| **预编辑串不可见（🔴 方案 A 新增）** | `color: transparent` 会让拼音候选阶段的下划线文字一并透明。`compositionstart` → 加 `.gta-composing`（textarea 恢复不透明 + highlighter 隐藏）；`compositionend` → 移除。详见 §4.3.3 |
| **组合期间的按键拦截** | `handleKeyDown` 首行 `if (e.nativeEvent.isComposing) return;`——否则 Enter（选词确认）、Escape（取消候选）会被补全逻辑吃掉，中文输入直接废掉 |
| `compositionend` 与 `onChange` 顺序 | 各浏览器不一致，需真机验证切回透明层时正文已同步（§4.3.3 验证清单） |

### 5.8 大文档性能

| 场景 | 处理 |
|------|------|
| 笔记内容 > 2000 字符 | ⚠️ I4 修复：前端发送前在光标附近截取（前 1200 + 后 300），实际发送 ≤ 1500 字符，Schema `max_length=2000` 永不会触发 422 |
| 光标前内容 > 800 字符 | Service 层 `_truncate_context` 截取光标前 ≤ 800 字符（段落边界优先）+ 光标后 ≤ 300 字符（N2：FIM 式） |
| 前端 `content` state 大 | React state 存完整字符串无性能问题（10K 字符串 < 100KB） |

---

## 6. 实施计划

### 6.1 阶段划分

#### 第一阶段：MVP（后端 + 前端核心）

| 任务 | 文件 | 工作量 |
|------|------|--------|
| 创建 `prompts/autocomplete_prompt.txt` | `prompts/` | 0.5h |
| 新增 `create_autocomplete_model()` 工厂函数 | `app/utils/factory.py` | 0.5h |
| `BackgroundInitManager` 阶段 1 建模型 **+ 阶段 2 注入 NoteService**（★ C5） | `main.py` | 0.5h |
| `NoteService` 新增 `autocomplete_model` 属性 + `ai_autocomplete()` 方法 | `app/services/note_service.py` | 1.5h |
| 扩展 `AutocompleteRequest` Schema + 新增 `AutocompleteResponse` | `app/schemas/note.py` | 0.5h |
| 改造 `autocomplete` router（接入 LLM + 限流） | `app/routers/note_router.py` | 0.5h |
| 前端新增 `useAutocomplete` hook | `front/src/hooks/useAutocomplete.ts` | 2h |
| ~~前端新增光标坐标计算工具~~ | ~~`front/src/utils/cursorPosition.ts`~~ | ~~1h~~ **取消（方案 A 无需测量）** |
| **★ 新增 `.gta-*` 共享样式**（两层排版一致 + 选区半透明 + scrollbar-gutter） | `front/src/styles/ghost-textarea.css` | 1h |
| **★ 新增 `<GhostTextArea>` 组件**（两层结构 + 滚动同步 + composition 切换） | `front/src/components/GhostTextArea.tsx` | 3h |
| **★ IME 真机验证**（微软拼音/搜狗 × Chrome/Edge/Safari） | — | 1.5h |
| `NoteEditor.tsx` 集成（替换两处 textarea + 按键处理） | `front/src/pages/NoteEditor.tsx` | 1.5h |
| 前端 API 层 + 类型扩展 | `front/src/api/notes.ts` + `front/src/types/api.ts` | 0.5h |
| **★ C2/C1：`get_current_user_id` 加 `request` 参数 + 限流参数顺序** | `app/utils/auth_utils.py` | 0.5h |
| **★ C4：`pricing.yaml` 新增 `qwen3.7-flash` 条目** | `config/pricing.yaml` | 0.2h |
| 联调测试 | — | 1h |
| **⚠️ I9 测试用例**（含 C1/C3/C4/C5 新增用例） | 见下方测试计划 | 2.5h |
| **小计** | | **~18.2h** |

> ✅ **实施记录（2026-09-09）：后端 MVP 已完成并端到端验证通过**——`factory.create_autocomplete_model()`、`main.py` 阶段 1 创建 + 阶段 2 注入（C5）、`NoteService.ai_autocomplete` / `_truncate_context` / `_postprocess_completion`（C3）、Schema 扩展、Router 改造（C1 参数顺序 + 限流 10）、C2 鉴权依赖修复、`tests/test_autocomplete.py`（21 条单测全绿）、**F17 trace 上下文修复**（§3.5）。真实 HTTP 实测：`POST /api/v1/note/autocomplete` → `{"completion":"两者互补","model":"qwen3.8-flash","latency_ms":671}`；`GET /usage/summary` → `by_stage={"stage":"autocomplete","calls":1,...}`、`by_model={"model":"qwen3.8-flash","cost_cny":0.0001}`。**前端（第二阶段）尚未开始**，下一步按 §4.3 / §4.4 实施，并先做 IME 真机验证。

> **工作量说明（C8 决策为 A-覆盖层后重估）**：v1.2 原估 12h 基于方案 B。改方案 A 后前端净增 ~6h：
>
> | 变化 | 增量 |
> |------|------|
> | 取消 `cursorPosition.ts` | −1h |
> | 新增 `.gta-*` 共享样式 | +1h |
> | 新增 `<GhostTextArea>` 组件 | +3h |
> | IME 真机验证（本项目为中文笔记应用，不可省） | +1.5h |
> | `NoteEditor.tsx` 集成从 2h 降至 1.5h（handler 收敛进组件） | −0.5h |
> | C1/C2/C4 后端修复 + 测试用例扩充（v1.3 引入） | +2.2h |
> | **合计** | **12h → ~18.2h** |
>
> **风险提示**：IME 那 1.5h 是**乐观值**。若 `compositionend` 与 `onChange` 的时序在某个输入法下表现异常，可能需要额外引入"组合结束后延迟一帧再切回透明"的补丁，最坏追加 2h。建议按 **18~20h** 排期。

#### 第二阶段：优化

| 任务 | 说明 | 工作量 |
|------|------|--------|
| 流式响应（SSE） | 降低首字节延迟，前端增量渲染 ghost text | 3h |
| txt 模式支持 | 调整 prompt 适配纯文本风格 | 1h |
| 移动端适配 | 底部浮层卡片展示补全建议 | 2h |
| 补全质量评估 | A/B 测试 + 采纳率埋点 | 2h |
| 请求合并（Redis 缓存） | 同用户 500ms 内重复请求返回缓存 | 1.5h |
| 前端用户设置 | 用户级开关（设置页切换 AI 补全开/关） | 1h |
| **小计** | | **~10.5h** |

### 6.2 测试计划（⚠️ I9 新增）

#### 后端单元测试（`tests/test_autocomplete.py`）

| 用例 | 测试目标 | 验证点 |
|------|---------|--------|
| `_truncate_context` 短内容 | 内容 ≤ max_before | before 原样返回，after ≤ max_after |
| `_truncate_context` 长内容 | before > max_before | 截取 ≤ 800 字符，优先段落边界 |
| `_truncate_context` 段落边界 | 截断点附近有 `\n\n` | before 从段落开头开始 |
| `_truncate_context` 返回元组 | N2 修复 | 返回 `(before, after)` 而非单个字符串 |
| `_postprocess_completion` 空串 | 模型返回空 | 返回空串 |
| `_postprocess_completion` 成对引号 | 模型返回 `"abc"` | 去除引号返回 `abc` |
| **`_postprocess_completion` 仅前引号** ★C3 | 模型返回 `"abc` | 原样返回 `"abc`，**不得**切掉尾字符 `c` |
| **`_postprocess_completion` 仅后引号** ★C3 | 模型返回 `abc"` | 原样返回 `abc"` |
| **`_postprocess_completion` 单字符** ★C3 | 模型返回 `"` | 不越界，原样返回 |
| **`_postprocess_completion` 中文弯引号** ★C3 | 模型返回 `“abc”` | 去除引号返回 `abc` |
| `_postprocess_completion` 超长 CJK | 模型返回 300 字符中文 | 截断至 200 字符（中文无空格，不得因 `rsplit(" ")` 返回空串） |
| `_postprocess_completion` 多行 | 模型返回多段解释 | 只取第一行 |
| Router 422 | content > 2000 字符 | 返回 422 |
| Router 429 | 10 次/分钟超限 | 返回 429 |
| **限流按 user_id 计数** ★C1 | 同 IP 两个不同 token 各发 8 次 | 均不触发 429（若按 IP 计数则第 11 次即 429） |
| **`request.state.user_id` 已写入** ★C1/C2 | 在 `rate_limit` 依赖内断言 | 读到真实 `user_id` 而非 `None` |
| Router 空 completion | 模型返回空 | 返回 `{"completion": ""}` |
| 功能开关 | `AUTOCOMPLETE_ENABLED=false` | 返回空 completion |
| **模型已注入 NoteService** ★C5 | 后台初始化完成后 | `init_manager.note_service.autocomplete_model is not None` |
| **pricing 条目命中** ★C4 | 补全调用后查 `model_traces` | 该行 cost > 0（条目名与运行时模型名一致） |

#### 前端 hook 测试（`useAutocomplete.test.ts`）

| 用例 | 验证点 |
|------|--------|
| 防抖时序 | 600ms 内多次调用只发一次请求 |
| AbortController | 新请求发出前取消上一次 |
| 组件卸载清理 | unmount 后无残留定时器/请求 |
| Tab 采纳返回 fullCursorPosition | `accept()` 返回 `{text, cursorPosition}` 且 cursorPosition 为完整内容偏移（N1） |
| dismiss 清理 | dismiss 后 state 全部重置 |

#### `<GhostTextArea>` 组件测试（★ v1.4 新增，`GhostTextArea.test.tsx`）

| 用例 | 验证点 |
|------|--------|
| **两层文本一致** | highlighter 的 `before + ghost + after` 拼接（去掉 ghost）等于 textarea 的 `value` |
| **ghost 插入位置** | `ghostAt=5` 时，highlighter 第一个文本节点长度为 5，ghost span 紧随其后 |
| **无补全时不切分** | `completion=''` 且 `isLoading=false` → highlighter 只有一个文本节点，无 ghost span |
| **滚动同步** | 触发 textarea `scroll`（`scrollTop=120`）→ highlighter `scrollTop === 120` |
| **IME：组合开始** | `compositionStart` → 容器含 `gta-composing` 类，且 `onInteract` 被调用一次 |
| **IME：组合结束** | `compositionEnd` → `gta-composing` 类被移除 |
| **IME：组合中不显示 ghost** | `isComposing=true` 且 `completion` 非空 → highlighter 中无 ghost span |
| **尾行不塌陷** | `value` 以 `\n` 结尾时，highlighter 文本以两个 `\n` 结尾（内容 + 补位） |
| **组合期间按键放行** | `keyDown` 且 `nativeEvent.isComposing=true` → `onKeyDown` 中不执行 Tab/Esc 分支 |

### 6.3 交付顺序

```
Week 1:
  Day 1-2: 后端（prompt + 工厂 + service + router + schema）
  Day 3-5: 前端（hook + 坐标计算 + NoteEditor 集成 + API 层）

Week 2:
  Day 1: 联调测试 + 边界情况修复
  Day 2-5: 第二阶段优化（按优先级排序）
```

### 6.4 验收标准

| 项目 | 标准 |
|------|------|
| 功能完整性 | 输入停顿 600ms 后出现 ghost text → Tab 采纳 / Esc 丢弃 / 继续输入丢弃 |
| 延迟 | 从停顿到 ghost text 出现 ≤ 1.5s（含网络 + 推理） |
| 稳定性 | 切换笔记无残留、网络断开无报错、IME 输入不异常触发 |
| 成本 | 单次补全 token ≈ 800-1000（中文 prompt ~500-800 + completion ~100-200，I10 修正），月成本可控 |
| 限流 | 单用户 10 次/分钟上限，超限静默降级 |

### 6.5 实施前置自检（已执行一次，脚本用后移除）

> **状态（2026-09-09）**：已按本节流程完成前置自检——`AUTOCOMPLETE_MODEL=qwen3.8-flash` 四项约束**全部通过**。自检脚本为一次性工具，验证通过后已移除；下表保留"怎么验、失败长什么样"，后续换模型时按同样逻辑重跑即可（直连 `ChatOpenAI` 或实现后的 `factory.create_autocomplete_model()`）。

| 阶段 | 验证内容 | 失败时的典型原因 |
|------|---------|-----------------|
| 一 | 模型名有效（非 404）、接受 `enable_thinking=False`（非 400）、延迟 ≤5s、`pricing.yaml` 命中同名条目 | 模型名拼错 / 模型不支持该参数 / 档位过慢 / 忘了加定价条目 |
| 二 | 端点返回非空 `completion` 与 `model`；`model_traces` 出现 `stage=autocomplete`；用量汇总该模型 `cost_cny > 0` | `autocomplete_model` 未注入 NoteService（C5）/ 未调 `set_trace_stage` / `TRACE_SINK` 不含 db / 定价条目缺失 |

**实测结果（2026-09-09，`.env` 的 `AUTOCOMPLETE_MODEL=qwen3.8-flash`）**：

```
[OK]   调用成功 | model=qwen3.8-flash | latency=938ms | tokens in/out=106/8
[OK]   延迟在预算内
[OK]   pricing.yaml 命中: qwen3.8-flash → in=0.0003 out=0.0006 元/1K
✅ 全部通过
```

> 首轮曾因 `pricing.yaml` 缺 `qwen3.8-flash` 条目而 `[FAIL]`（成本恒为 0），补条目后转绿——**这正是 C4 同类问题的现场复现**，也说明该自检值得在换模型时重跑一次。

> ⚠️ 阶段一为直连调用，**不会**写入 `model_traces`（DbSink 只运行在后端进程内）——trace 与计费必须用端到端方式验证：实现后调一次 `/note/autocomplete`，再查 `/usage/summary` 的 `by_stage`（应出现 `autocomplete`）与 `by_model`（该模型 `cost_cny > 0`）。

---

## 附录 A：文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `prompts/autocomplete_prompt.txt` | **新增** | 补全 prompt 模板 |
| `app/utils/factory.py` | 修改 | 新增 `create_autocomplete_model()`（I8：检查 `AUTOCOMPLETE_ENABLED` 开关；C4：默认模型名 `qwen3.7-flash`） |
| `app/utils/auth_utils.py` | 修改 | ⚠️ S3+**C2**：`get_current_user_id` **先加 `request: Request` 参数**（现签名无此参数），再写 `request.state.user_id = user_id` |
| `main.py` | 修改 | ⚠️ **C5**：① `_init_models`（阶段 1）新增 `self.autocomplete_model = create_autocomplete_model()`；② **`_init_services`（阶段 2）的 `NoteService(...)` 构造处补 `autocomplete_model=self.autocomplete_model`** ——漏掉②则功能永久静默失效 |
| `config/pricing.yaml` | 修改 | ⚠️ **C4**（修正 I1）：新增 **`qwen3.7-flash`** 条目（非 `qwen3-flash`），与现有 `qwen3.7-flash-2026-07-15` 并存 |
| `app/services/note_service.py` | 修改 | 新增 `autocomplete_model` 参数（默认 `None`）+ 模块级 `_QUOTE_CHARS` 常量（C3）+ `ai_autocomplete()` / `_truncate_context()` / `_postprocess_completion()` |
| `app/schemas/note.py` | 修改 | 扩展 `AutocompleteRequest` + 新增 `AutocompleteResponse` |
| `app/routers/note_router.py` | 修改 | 改造 `autocomplete` 端点（接入 service + 限流）；⚠️ **C1**：限流用参数顺序写法，`user_id` 必须排在 `rate_limit` 之前 |
| `front/src/hooks/useAutocomplete.ts` | **新增** | ✅ 已实施（薄片版）：防抖 + 最小请求间隔 7s + 状态管理（含 `fullCursorPosition`，N1） |
| `front/src/hooks/__tests__/useAutocomplete.test.tsx` | **新增** | ✅ 已实施：7 条单测（防抖合并 / 最小间隔 / 触发条件 / accept / dismiss / 空补全 / 卸载清理） |
| ~~`front/src/utils/cursorPosition.ts`~~ | ~~新增~~ | **❌ v1.4 取消**——方案 A 无需像素测量，I7/C6 随之消失 |
| `front/src/components/GhostTextArea.tsx` | **新增** | ✅ 已实施（方案 A）：透明 textarea + highlighter 两层结构；滚动同步、composition 切换、选区半透明、`enabled=false` 降级均封装于此 |
| `front/src/styles/ghost-textarea.css` | **新增** | ✅ 已实施：`.gta-typography` / `.gta-mono` / `.gta-textarea` / `.gta-highlighter` / `.gta-ghost` / `.gta-composing` |
| `front/src/components/__tests__/GhostTextArea.test.tsx` | **新增** | ✅ 已实施：10 条组件测试（两层一致 / ghost 位置 / 滚动同步 / IME 切换 / 尾行 / 降级） |
| `front/src/pages/NoteEditor.tsx` | 修改 | ✅ 已实施：两处 textarea 换成 `<GhostTextArea>`（txt 传 `enabled={false}`）+ `handleContentChange` / `handleEditorKeyDown`；底部建议条已删除 |
| `front/src/api/notes.ts` | 修改 | 扩展 `autocomplete` 方法（title + AbortSignal） |
| `front/src/types/api.ts` | 修改 | 新增 `AutocompleteRequest` / `AutocompleteResponse` |
| ~~`scripts/manual/verify_autocomplete_model.py`~~ | ~~新增~~ | 一次性自检脚本：2026-09-09 验证通过后已移除（结论与复现方法见 §6.5） |

## 附录 B：配置项汇总

| 配置 | 位置 | 默认值 | 说明 |
|------|------|--------|------|
| `AUTOCOMPLETE_ENABLED` | `.env` | `true` | ⚠️ I8 新增：功能总开关，`false` 时工厂函数返回 `None`，快速止血 |
| `AUTOCOMPLETE_MODEL` | `.env` | `qwen3.7-flash` | 补全专用模型名，**可独立配置、与 `CLASSIFIER_MODEL` 解耦**（§3.3）；须与 `pricing.yaml` 条目名一字不差，且模型须支持 `enable_thinking=False` |
| `AUTOCOMPLETE_TIMEOUT` | `.env` | `5.0` | LLM 推理超时（秒，F9 起为真实 env） |
| `AUTOCOMPLETE_MAX_TOKENS` | `.env` | `200` | 生成 token 上限（F9 起为真实 env；字符上限见下行） |
| `AUTOCOMPLETE_MAX_CONTEXT` | `.env` | `800` | 光标前上下文截取字符数（F9 起为真实 env） |
| `AUTOCOMPLETE_MAX_COMPLETION` | `.env` | `200` | 补全结果字符上限（F9 起为真实 env） |
| `debounceMs` | 前端代码层 | `600ms` | 停顿触发延迟 |
| `endpoint_limit` | `rate_limit.py` | `10` | 每分钟限流次数（补全端点；建议后续改为 `AUTOCOMPLETE_RATE_LIMIT` 可配，见 §3.4） |

---

## 附录 C：v1.0 → v1.1 修订记录

| # | 级别 | 问题 | 修复内容 |
|---|------|------|----------|
| **S1** | 🔴 严重 | Ghost text 永不显示——坐标计算时机缺陷 | hook state 补存 `cursorPosition`；NoteEditor 加 `useEffect` 在 completion 到达时自动计算坐标（取代 `handleSelect`） |
| **S2** | 🔴 严重 | Tab 采纳追加到末尾，与补全语义矛盾 | `accept()` 返回 `{text, cursorPosition}`；`handleKeyDown` 改为 `prev.slice(0, pos) + text + prev.slice(pos)` 光标处插入 |
| **S3** | 🔴 严重 | 限流按 IP 而非 user_id | `get_current_user_id` 增加 `request.state.user_id = user_id`；限流改用 router 级 `dependencies=[Depends(...)]`（与项目 5 处惯例一致） |
| I1 | 🟡 | qwen-flash 不在 pricing.yaml + 模型名不一致 | 默认值改为 `qwen3-flash`；附录 A 补 `config/pricing.yaml` 新增条目 |
| I2 | 🟡 | 双 textarea 未覆盖 | 抽出 `renderGhostOverlay()` 共享函数；txt/md 两个 textarea 均挂载；editorMode=preview 时 dismiss |
| I3 | 🟡 | 陈旧补全（请求中光标移动不 abort） | textarea 挂 `onClick`/`onKeyUp`(方向键)/`onBlur` → `dismiss()` |
| I4 | 🟡 | content>10000 直接 422 + 光标后内容不参与 | 前端发送前截取（前 1200 + 后 300）；Schema `max_length` 改为 2000；后端 `_truncate_context` 进一步截取光标前 ≤ 800 |
| I5 | 🟡 | `_truncate_context` 两个不一致版本 | 删除 §3.3 版，统一使用 §3.6 版（`para_start > max_chars // 2`） |
| I6 | 🟡 | 前端触发条件 `content.length < 10` 与规格不符 | 改为 `cursorPosition < 10` |
| I7 | 🟡 | 镜像测量 Firefox 兼容 + ghost span 溢出 | `computedStyle.font` 改为逐项复制；ghost span 加 `max-width` + `white-space: pre-wrap` + `overflow: hidden` |
| I8 | 🟡 | 缺功能开关 | 新增 `AUTOCOMPLETE_ENABLED` env 变量；工厂函数检查开关返回 None 即降级 |
| I9 | 🟡 | 测试计划缺失 | 新增 §6.2 测试计划（后端 11 个 pytest + 前端 5 个 hook 测试） |
| I10 | 🟡 | 成本估算偏乐观 | 修正为 800-1000 tokens/次（中文 800 字符 ≈ 500-800 prompt tokens） |
| I11 | 🟡 | 限流写法与项目惯例不一致 | 改为 `dependencies=[Depends(rate_limit(...))]` router 级装饰器 |
| I12 | 🟡 | 客户端 abort 不省服务端成本 | §5.1 注明 DashScope 调用照常执行并计费 |

---

## 附录 D：v1.1 → v1.2 修订记录

| # | 级别 | 问题 | 修复内容 |
|---|------|------|----------|
| **N1** | 🔴 严重 | `cursorPosition` 双语义混用——I4 截断偏移与 S1/S2 完整内容偏移冲突 | hook state 拆分为 `cursorPosition`（截断偏移，API 用）+ `fullCursorPosition`（完整偏移，ghost text 坐标 + Tab 插入用）；`onContentChange` 接受 4 个参数；S1 effect / Tab 插入 / `setSelectionRange` 全部改用 `fullCursorPosition` |
| **N2** | 🟡 中等 | I4「光标后内容参与上下文」承诺未兑现——后端 `_truncate_context` 只取 `content[:cursor_position]` | 采用 FIM 式 prompt：`{context_before}` + `{context_after}`；`_truncate_context` 返回 `(before, after)` 元组；prompt 模板明确「在光标前末尾续写，与光标后内容自然衔接」 |
| 低优 1 | 🟢 | loading 指示是死代码 | S1 effect 条件改为 `(completion \|\| isLoading) && fullCursorPosition > 0`；请求发起时写入 `fullCursorPosition` |
| 低优 2 | 🟢 | 滚动同步缺失 | 改为 `onScroll` 直接 `dismiss()`（两个 textarea 均已挂载） |
| 低优 3 | 🟢 | JSX 属性区 `{/* I3 */}` 注释非法 | 移除属性行内注释 |
| 低优 4 | 🟢 | txt 模式 handler 为死代码 | 注释说明为「第二阶段启用预留」，保留不动 |

---

## 附录 E：v1.2 → v1.3 修订记录（对照实际代码核对）

> 与附录 C/D 的区别：C/D 是文档内部自洽性复审，本轮是**逐条对照仓库实际代码核对**，含 FastAPI 依赖顺序实测与 Python AST 实测。

### E.1 核对通过项（方案对现状的描述属实，可直接依赖）

| 方案主张 | 核对结果 |
|---------|---------|
| `AutocompleteRequest` 已存在于 `schemas/note.py:94-97` | ✅ 属实 |
| `POST /note/autocomplete` 返回硬编码占位（`note_router.py:310-317`） | ✅ 属实 |
| `prompt.yaml:6` 已配 `autocomplete`，但 `prompts/autocomplete_prompt.txt` 不存在 | ✅ 属实（另：`write_assistant` / `report` / `reorder` / `review_question` 四个模板同样"配了没文件"，属既有状态） |
| 前端 `notes.ts:64-65` / `endpoints.ts:55` 已就绪 | ✅ 属实 |
| `create_classifier_model()` 可作为 `create_autocomplete_model()` 的模板 | ✅ 属实（`factory.py:161-209`，含 `http_async_client` / `extra_body` / `TRACE_CALLBACK` 全部可照搬） |
| S3：`rate_limit.py:154` 读 `request.state.user_id` 且该属性从未被赋值 | ✅ 属实 |
| 项目现有 5 处限流均为 router 级 `dependencies` 写法 | ✅ 属实（`chat.py`×3、`user.py`×3、`knowledge_router.py`×1，实为 7 处） |
| `NoteEditor` 以 `key={note?.id ?? 'new'}` 重挂载 | ✅ 属实（`NoteEditor.tsx:9` 注释） |
| 前端使用 axios（`axios.isCancel` 可用） | ✅ 属实（`api/client.ts:10`） |
| `tuple[str, str]` 类型注解可用 | ✅ 属实（Python 3.12.10） |

### E.2 本轮修订

| # | 级别 | 问题 | 修复内容 |
|---|------|------|----------|
| **C1** | 🔴 阻断 | **I11 使 S3 修复失效（文档自相矛盾）**。FastAPI 把 router 级 `dependencies` `insert(0,…)` 到依赖链最前，**先于** endpoint 的 `Depends(get_current_user_id)` 执行 → `request.state.user_id` 仍为 `None`，限流照旧按 IP。本仓库 FastAPI 0.139.2 实测三种写法确认 | §3.4/§3.7 撤销 I11，改回 `rate_limit.py:131` docstring 的参数顺序写法，且 `user_id` 必须排在 `rate_limit` 之前；补充说明现有 7 处 router 级用法**不会**因 S3 受益，另立技术债 |
| **C2** | 🔴 阻断 | **S3 的"一行改动"不成立**。`auth_utils.py:381-384` 现签名为 `(authorization=Header(None), db=Depends(get_db))`，**无 `request` 参数** | §3.4 给出完整签名改造代码，先加 `request: Request` 再写 `state` |
| **C3** | 🔴 阻断 | **`_postprocess_completion` 引号判断是静默逻辑错误**。`('"', "'", '"', ''')` 中的 `'''` 起始三引号字符串，吞掉 `and text[-1] in (...)` 整段。AST 实测：`BoolOp` 仅剩 2 分支，`text[-1]` 检查消失。**语法能过、v1.2 既有测试用例也能过**，但 `"abc` 会被切成 `"ab` | §3.6 引号集合提为模块级 `_QUOTE_CHARS` 常量 + `len(text) >= 2` 越界保护；CJK 截断改为 `" " in head` 判定；§6.2 新增 4 条单侧引号/单字符/弯引号用例 |
| **C4** | 🟡 | **I1 修正方向错误**。`.env` 实际 `CLASSIFIER_MODEL=qwen3.7-flash`（覆盖了代码默认值 `qwen3-flash`），而 `pricing.yaml:19` 是 `qwen3.7-flash-2026-07-15` → 新增 `qwen3-flash` 条目是死条目。另 §3.3 称主模型为 `qwen-max`，实际 `.env` 为 `qwen3.8-max` | §3.3/附录 A/附录 B 统一改为 `qwen3.7-flash`；顺带指出**现有分类器/Plan 模型成本统计已恒为 0**，补此条目可一并修好 |
| **C5** | 🟡 | **模型接线点遗漏**。模型在 `main.py:124 _init_models`（阶段 1）创建，NoteService 在 `main.py:164 _init_services`（阶段 2）构造。v1.2 只写"阶段 1 初始化"，未提阶段 2 传参 → `autocomplete_model` 恒为 `None`，功能**静默失效且无任何日志** | §6.1/附录 A 明确列出两处改动；§6.2 新增注入断言用例 |
| **C6** | 🟡 | **ghost span 定位原点错位**。`NoteEditor.tsx` 全文无 `position: relative`；§4.4 把 md overlay 放在**非 positioned** 的左栏 div 内 → span 相对最外层 `relative` 容器定位，`edit` 模式下被"编辑"标题条（≈28px）顶偏一行 | §4.3 坐标函数补 `textarea.offsetTop / offsetLeft` 换算；并给出"把 `relative` 加在 textarea 直接父容器"的替代修法 |
| **C7** | 🟢 | `format_prompt` 位于 try 块外，模板缺失/未配置会抛 `FileNotFoundError`/`KeyError` → 500，而非设计承诺的静默降级 | §3.6 用 try 包裹并降级返回空 completion |
| **C8** | 🟠 **待决策** | **方案 B 渲染与 N2 的 FIM 中部补全互斥**。ghost text 绝对定位叠加，但光标后正文仍在原位渲染 → 中部补全会遮挡用户已有文字；而 `context_after` 正是为中部补全而加，补得越准遮挡越重 | §2.3 新增 C8 决策块（B-限定 / A-覆盖层 / C-状态栏 三选一）；§6.1 标注 12h 估算在决策前不成立 |

### E.3 未纳入本次范围的既有问题

| 问题 | 说明 |
|------|------|
| 7 处 router 级限流全部按 IP 计数 | 其中 `/auth/register`、`/auth/login`、`/auth/send-code` **本就应按 IP**（防暴力破解），无需改；其余 4 处（`/chat/query`、`/chat/rag`、`/chat/files`、`/knowledge/upload`）建议单独立项改为参数顺序写法 |
| 分类器 / Plan 模型成本统计恒为 0 | 由 C4 的条目名不匹配导致，补 `qwen3.7-flash` 条目后自动修复 |
| `prompt.yaml` 中 4 个模板文件缺失 | `write_assistant` / `report` / `reorder` / `review_question`，与本功能无关 |

### E.4 实施前置条件

按以下顺序推进，前两项未完成不建议动工：

1. **决策 C8**（产品）——决定前端实现方式与工作量
2. **确认 C4 的 `pricing.yaml` 条目名**——需与线上实际 `AUTOCOMPLETE_MODEL` 取值精确一致
3. C1/C2/C3/C5/C6/C7 按上文正文修订实施
4. 联调时优先验证 §6.2 中标 ★ 的 6 条新增用例（它们对应本轮全部阻断项）

---

## 附录 F：v1.3 → v1.4 修订记录（C8 决策：方案 A 覆盖层）

> **决策**：2026-08-26，C8 选定 **A-覆盖层**（透明 textarea + 底层 highlighter div，含滚动/选区/IME 同步）。
> **影响面**：仅前端。后端 C1-C7 全部修订原样沿用，**§3 章一字未动**。

### F.1 本轮变更

| # | 章节 | 变更 |
|---|------|------|
| **F1** | §2.3 | 方案选型由 B 改为 **A**；重写实现要点为 8 条（两层盒模型一致 / 透明化 / 选区半透明 / 滚动同步 / scrollbar-gutter / 尾行塌陷 / IME / ghost 样式）；C8 决策块改为已决策留档 |
| **F2** | §4.1 | 架构图重画为两层结构；新增 `<GhostTextArea>` 组件化决策说明（同时解决 I2 双 textarea 与 C6 定位原点） |
| **F3** | §4.3 | **整节替换**。新增 §4.3.1 共享 CSS、§4.3.2 组件实现、§4.3.3 IME 专章、§4.3.4 边界表、§4.3.5 降级开关 |
| **F4** | §4.4 | 两处裸 `<textarea>` 换成 `<GhostTextArea>`；删除 `ghostPos` state / 坐标 `useEffect` / `renderGhostOverlay()`；`handleKeyDown` 补 `isComposing` 早退与 Tab 放行；附 v1.2↔v1.4 差异对照表 |
| **F5** | §5.7 | **结论反转**：v1.3 的「IME 无需特殊处理」在方案 A 下不成立，重写为 4 条处理项 |
| **F6** | §6.1 | 取消坐标工具（−1h），新增样式（+1h）/ 组件（+3h）/ IME 真机验证（+1.5h）；小计 **12h → ~18.2h**，建议按 18~20h 排期 |
| **F7** | §6.2 | 新增 `<GhostTextArea>` 组件测试 9 条（两层一致性 / ghost 位置 / 滚动同步 / IME ×3 / 尾行 / 按键放行） |
| **F8** | 附录 A | `cursorPosition.ts` 划除；新增 `GhostTextArea.tsx` + `ghost-textarea.css`；`NoteEditor.tsx` 说明更新 |

### F.2 因方案 A 而**自动消失**的历史问题

| 原编号 | 原问题 | 消失原因 |
|--------|--------|---------|
| **I7**（v1.1） | 镜像测量 div 在 Firefox 下 `computedStyle.font` 返回空串，需逐项复制样式 | 方案 A 不做任何像素测量 |
| **I7**（后半） | ghost span 200 字符横向溢出，需 `max-width` + `overflow: hidden` | ghost 在正常文档流内，随正文自然换行 |
| **S1**（v1.1） | ghost text 永不显示——坐标计算时机缺陷 | 无坐标可算 |
| **C6**（v1.3） | ghost span 定位原点错位（`offsetTop` 未补偿） | 两层 `absolute inset-0`，坐标系天然重合 |
| **低优 2**（v1.2） | 滚动同步缺失，改为 `onScroll → dismiss()` | 改为真正的滚动同步，ghost 不再因滚动消失 |

### F.3 因方案 A 而**新增**的风险

| 风险 | 级别 | 缓解 |
|------|------|------|
| **IME 预编辑串透明** —— 中文输入时看不见拼音候选 | 🔴 最高 | `composition` 事件切换渲染模式（§4.3.3）；**必须真机验证** 微软拼音/搜狗 × Chrome/Edge/Safari |
| **爆炸半径扩大** —— 双层结构对所有用户常驻生效，排版漂移会损伤与 AI 无关的日常打字 | 🟠 高 | §4.3.5 降级开关：`enabled=false` 时退回原生 textarea；验收须做"关闭开关后与改造前逐像素一致"的截图 diff |
| **滚动条宽度导致换行漂移** | 🟡 中 | 两层统一 `scrollbar-gutter: stable` |
| **选区盖住底层文字** | 🟡 中 | `.gta-textarea::selection` 半透明 |
| **尾行塌陷** | 🟢 低 | highlighter 末尾补 `{'\n'}` |
| **后续维护者传错 className** —— 传入 `p-*`/`text-*` 导致两层漂移 | 🟢 低 | 组件源码顶部写死约定 + §4.4 末尾注明 |

### F.4 实施前置条件（更新 E.4）

1. ~~决策 C8~~ ✅ **已完成**（方案 A）
2. **确认 C4 的 `pricing.yaml` 条目名**——需与线上实际 `AUTOCOMPLETE_MODEL` 取值精确一致（唯一剩余的前置阻塞）
3. 后端按 C1/C2/C3/C5/C7 实施（§3 章）
4. 前端按 §4.3 / §4.4 实施，**先做 `<GhostTextArea>` 的 IME 真机验证再接补全逻辑**——若 IME 方案走不通，应尽早回退到 C8 的 B-限定选项，避免在补全逻辑上白投工时
5. 验收必须包含 §4.3.5 的"关闭开关后逐像素一致"截图 diff

---

## 附录 G：v1.4 → v1.5 评审记录（逐条对照代码复核）

> **评审范围**：全文 + 附录 A–F 的**全部代码断言**，逐条比对仓库真实代码与依赖版本（FastAPI `0.139.2`、`@tiptap/react` `3.28.0`、`prompts/` 目录实况）。
> **结论**：事实基础扎实——G.1 核对项**无一处偏差**；本轮修正 **F1 / F2 / F7 / F15**，**F3–F6、F8–F14 登记为实施时处理**。

### G.1 核对通过项（文档主张 → 实测）

| 文档主张 | 核对结果 |
|---------|---------|
| `schemas/note.py:94-97` `AutocompleteRequest`（无 `title` / `max_length`） | ✅ 属实 |
| `note_router.py:310-317` 返回硬编码占位 | ✅ 属实 |
| `note_service.py:9` 注释提及、`__init__(vector_store, chat_model)` 无补全模型 | ✅ 属实（C5 成立） |
| `prompt.yaml:6` 已配、`prompts/autocomplete_prompt.txt` 不存在；另有 4 个模板同样缺失 | ✅ 属实（`prompts/` 实存 7 个 txt） |
| `notes.ts:64-65` / `endpoints.ts:55` / `types/api.ts` 无类型 | ✅ 属实 |
| `NoteEditor.tsx:9` key 重挂载注释 / `:310`+`:330` 两处裸 textarea / `117-146` 工具栏 | ✅ 属实 |
| `factory.py:177` 默认 `qwen3-flash`、`.env` 实际 `qwen3.7-flash`、`pricing.yaml:19` 为带日期后缀条目 | ✅ 属实 → **C4「分类器/Plan 成本恒为 0」成立** |
| `auth_utils.py:381-384` 无 `request` 参数（C2） | ✅ 属实 |
| `rate_limit.py:154` 读 `request.state.user_id`，全仓库无赋值（S3） | ✅ 属实（`grep state.user_id` 零命中） |
| `main.py:124` / `:164` / `:171 NoteService(...)`（C5） | ✅ 属实 |
| `format_prompt` 抛 `FileNotFoundError` / `KeyError`（C7） | ✅ 属实 |
| FastAPI `0.139.2`、`client.ts:10` axios、`@tiptap/react` 已装未用 | ✅ 属实（tiptap 仅存在于 `index.css` 与 AboutUs 文案） |

### G.2 本轮已修

| # | 级别 | 问题 | 证据 | 修复 |
|---|------|------|------|------|
| **F1** | 🔴 P1 | `.gta-typography` 无条件 `font-family: var(--font-mono)` → **txt 模式字体被静默改成等宽**（与 AI 无关的体验回归） | `NoteEditor.tsx:315` 无 font 类（继承 `--font-body`）vs `:335` 带 `font-[var(--font-mono)]`；`mono` prop 只控制 Tailwind 类，压不住 CSS 里的硬编码 | §4.3.1 移除 `.gta-typography` 的 `font-family`，新增 `.gta-mono` 修饰类；§4.3.2 组件改用 `gta-mono`；§4.3.4 补约束说明 |
| **F2** | 🔴 P1 | §4.3.5 的降级开关 `enabled` **未接线**：`GhostTextAreaProps` 无该属性、§4.4 未传参、txt 模式仍走双层结构 | 文档内部不一致（§4.3.5 有代码，§4.3.2 / §4.4 无） | §4.3.2 增加 `enabled?: boolean` + 早退分支（**置于全部 Hook 之后**，否则违反 Rules of Hooks）；降级路径复刻 `:315` / `:335` 样式差异；§4.4 定义 `isMobile` 并给两处传 `enabled={!isTxt && !isMobile}`（txt 恒 `false`）；§4.3.5 改为引用实现 |
| **F7** | 🟠 P2 | C4 修正后仍有 4 处旧模型名残留，其中 §3.5 与 C4 **直接矛盾** | §1.2 / §3.2 / §3.5（"需确保包含 `qwen3-flash` 条目"）/ §5.6（`qwen3-flash` + `qwen-max`） | 4 处统一为 `qwen3.7-flash` / `qwen3.8-max`；附录 C 第 1300 行属历史记录，保留不动 |
| **F15** | 🟠 P2 | §4.4 两处 **JSX 属性区注释非法**（`mono={false} {/* ... */}`、`mono {/* ... */}`），照抄直接编译失败——附录 D 低优 3 修过同类问题，此处漏网 | 示例代码 txt / md 两处 `<GhostTextArea>` | 注释移出属性区（属性区只允许 `属性=值`）；顺带补齐 `enabled` 传参 |
| **F16** | 🟠 P2 | `create_autocomplete_model()` 以 `else → Ollama` 兜底：`MODEL_PROVIDER` 若新增 `openai`（见《DashScope迁移OpenAI大模型接入设计方案》），补全会**静默构造打 Ollama 地址的客户端**（表现为连接失败 / 超时） | 与迁移文档 `create_outline_model()` 同类缺陷（该文档 F2）；示例代码仅 DASHSCOPE + 隐式 Ollama 两分支 | §3.3 示例改为显式 `OLLAMA` 分支 + 未知 provider `raise ValueError`，并注明 OpenAI 分支接入点；新增「补全模型可独立配置」小节（4 条硬约束 + 选型清单） |
| **F17** | 🔴 P1 | **trace 上下文缺失（实施阶段实测发现）**：note 类端点从不调用 `set_trace_context`，而 `set_trace_stage` 在无上下文时是空操作（`model_trace.py:55-57`）→ `model_traces.user_id=""`、`stage="chat"`，`/usage/summary` 按用户过滤后**看不到补全调用**（§3.5 原称"无需额外代码"不成立） | 真实 HTTP 验证：首次复验 `by_stage`/`by_model` 均为空、`total_cost_cny=0` | `note_router.py` 调 service 前 `set_trace_context(new_request_id(), user_id=user_id, stage="autocomplete")`，`finally: clear_trace_context()`；复验 `by_stage={stage:autocomplete,calls:1}`、`by_model cost_cny=0.0001`（§3.5） |
| **F3** | 🟠 P2 | 工具栏 `wrapSelection` / `prefixLine` 直接 `setContent`，绕过 `handleContentChange` → 建议条不消失、插入点变陈旧，此时 Tab 会**插到错误位置** | 前端实施阶段复核确认（薄片版同样受影响） | 两个函数内首行调用 `autocomplete.dismiss()`（`NoteEditor.tsx`），依赖数组补 `autocomplete` |
| **F4** | 🟠 P2 | 前端 axios 全局 `timeout: 30000`（`client.ts:17`），补全的 5s 超时只在后端；降级到主模型时后端 120s → 用户白等 30s | 前端实施阶段复核确认 | `notes.ts` 的 `autocomplete` 请求传 `{ timeout: 5000, ...config }` |
| **F11** | 🟢 P3 | 引号去除不要求成对匹配（`"abc'` 会被剥成 `abc`） | 前端实施阶段复核确认 | 后端改 `_QUOTE_PAIRS` 映射判定（`_QUOTE_PAIRS.get(text[0]) == text[-1]`），补"首尾不成对不剥离"用例 |
| **F18** | 🟠 P2 | **ghost span 未按 `showGhost` 门控（实施阶段组件测试发现）**：§4.3.2 原代码 `{completion && <span className="gta-ghost">…}` 在 IME 组合期（`showGhost=false`，highlighter 已 `visibility:hidden`）仍会渲染，且因文本未切分而落在**文末**——视觉被隐藏掩盖、语义错误，一旦调整隐藏方式就会暴露 | 组件测试 `IME：组合开始切层并丢弃建议` 断言 `ghost() === null` 失败 | `GhostTextArea.tsx` 与 §4.3.2 示例统一改为 `{showGhost && completion && …}` / `{showGhost && isLoading && …}` |
| **F5** | 🟢 P3 | `max_tokens=200` 是 **token**，文档按"200 字符"描述 | 收尾阶段复核 | §2.1 / 附录 B 注明"token 是安全上限、字符契约由后处理保证" |
| **F6** | 🟠 P2 | 缺 `DASHSCOPE_API_KEY` 时回退主模型（120s 超时 + 主模型单价），与"高频、可降级"定位冲突 | 收尾阶段复核 | `factory.create_autocomplete_model()` 缺 Key 直接返回 `None` + warning；补单测 |
| **F8** | 🟢 P3 | 前端传 1200+300、后端只用 800+300，约 400 字符白传 | 收尾阶段复核 | `NoteEditor.tsx` 截取改 **800+300**，与 `AUTOCOMPLETE_MAX_CONTEXT` 对齐 |
| **F9** | 🟢 P3 | 附录 B 的 `AUTOCOMPLETE_TIMEOUT` / `MAX_CONTEXT` / `MAX_COMPLETION` 名为"配置项"、实为硬编码 | 收尾阶段复核 | 全部改为**真实 env**（另加 `AUTOCOMPLETE_MAX_TOKENS`）；`.env.example` 与附录 B 同步 |
| **F10** | 🟢 P3 | `scrollbar-gutter` 浏览器支持未注明 | 收尾阶段复核 | CSS 注释注明 Chrome 94+ / Firefox 97+ / Safari 18.2+ 与降级表现 |
| **F12** | 🟢 P3 | 缺隐私 / 提示注入说明 | 收尾阶段复核 | §5.1 新增"隐私 / 提示注入"行：输出仅作纯文本插入、不执行其中指令 |
| **F13** | 🟢 P3 | 测试覆盖缺口 | 收尾阶段复核 | ✅ 已覆盖：`_truncate_context` 段落边界"落在前半段退回硬截断" + `_postprocess_completion` "首尾引号不成对" |
| **F14** | 🟢 P3 | 文档 §4.4 示例的 `exhaustive-deps` 告警 | 收尾阶段复核 | 实际实现未采用该写法（覆盖层与 hook 均无相关 effect）→ **N/A** |

### G.3 登记待处理

> **当前无待处理项**——F3–F6、F8–F14、F17、F18 已全部落地或判定 N/A；F7/F15/F16 在 G.2。新发现的问题请追加到本表。

### G.4 实施前置条件（更新 F.4）

1. ~~决策 C8~~ ✅ 已完成（方案 A）
2. ~~确认 `pricing.yaml` 条目名~~ ✅ **已完成**——`.env` 的 `AUTOCOMPLETE_MODEL=qwen3.8-flash` 与新增的 `pricing.yaml` 同名条目已对齐，§6.5 自检四项全绿（2026-09-09）
3. 后端按 C1/C2/C3/C5/C7 实施（§3 章）
4. 前端按 §4.3 / §4.4 实施（**含本轮 F1/F2 修正**），先做 `<GhostTextArea>` 的 IME 真机验证再接补全逻辑
5. 验收必须包含 §4.3.5 的"关闭开关后逐像素一致"截图 diff——**F1 修正后该验收才真正成立**
6. F3–F6 建议在联调阶段一并处理（F3 / F4 直接影响用户可见行为）

---

## 附录 H：IME 真机验证清单（方案 A 启动前置）

> **目的**：方案 A（§4.3 透明 textarea + highlighter 覆盖层）的头号风险是"IME 预编辑串随 textarea 一起透明"。本清单在投入 ~14h 前端改造**之前**验证该风险是否可解——**验证不通过就不做方案 A**，保留薄片版或退回 C8 的 B-限定。
>
> **现状**：薄片版（建议条）**不需要**本清单即可上线；下列项只决定"是否值得做内联 ghost text"。

### H.1 环境矩阵（每格都要过）

| 输入法 \ 浏览器 | Chrome | Edge | Safari |
|---|---|---|---|
| 微软拼音 | ☐ | ☐ | — |
| 搜狗拼音 | ☐ | ☐ | ☐ |
| macOS 原生拼音 | — | — | ☐ |

### H.2 方案 A 检查项

| # | 操作 | 期望 | 失败表现 |
|---|---|---|---|
| 1 | 输入拼音（不选词） | **预编辑串可见**（带下划线），光标位置正确 | 屏幕上看不到正在输入的拼音 → 方案 A 直接不可用 |
| 2 | 组合期间按 Enter（选词） | 正常上屏，不触发采纳/异常换行 | Enter 被补全逻辑吃掉 |
| 3 | 组合期间按 Esc（取消候选） | 取消候选，不丢弃/采纳补全 | Esc 被补全逻辑吃掉 |
| 4 | `compositionend` 后立即继续输入 | 切回透明层时正文已同步，无闪烁/错位 | 切回瞬间文字跳动或丢失 |
| 5 | 长文本（>1 屏）滚动 | 两层同步滚动，无错位 | highlighter 与 textarea 滚动不同步 |
| 6 | 选中一段文字 | 选区半透明，底层文字可见 | 选区全黑遮住文字 |
| 7 | 出现纵向滚动条时观察换行 | 两层换行位置一致 | 文字整体错位（`scrollbar-gutter` 未生效） |
| 8 | 末尾输入换行 | 尾行不塌陷 | 末尾空行高度不一致 |
| 9 | 粘贴 / Ctrl+Z 撤销 | 与改造前一致 | 撤销栈异常 |
| 10 | 关闭 `AUTOCOMPLETE_ENABLED` 后截图对比 | 与改造前**逐像素一致** | 字体/行高/间距漂移 |

### H.3 判定规则

- **全部通过** → 启动方案 A（§4.3），薄片版保留为降级形态；
- **第 1/2/3 项任一失败** → **不做方案 A**：采用 C8 的 B-限定（仅光标后为空白时内联显示），或长期保留薄片版；
- **仅第 5~9 项失败** → 可做方案 A，但每项追加约 1~2h 修复工时。

### H.4 薄片版同时要验的（与方案 A 无关，现在就能验）

| # | 操作 | 期望 |
|---|---|---|
| A1 | 输入中文时按 Enter / Esc | 不误触发采纳/丢弃（`isComposing` 早退生效） |
| A2 | 连续写作 1 分钟 | 建议出现 ≤ 9 次（7s 最小间隔生效），不出现"时有时无" |
| A3 | 停顿 600ms 后 | 1.5s 内出现建议条（含网络 + 推理） |
| A4 | 点击工具栏加粗 / 标题 | 建议条立即消失（F3 修复生效） |
| A5 | 停掉后端后继续写作 | 静默无报错、编辑器可用（5s 超时生效，F4 修复） |
