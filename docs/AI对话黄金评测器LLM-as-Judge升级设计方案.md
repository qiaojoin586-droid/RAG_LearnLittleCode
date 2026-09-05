# AI 对话黄金评测器 LLM-as-Judge 升级设计方案（双评委评测）

> 版本：v1.0 ｜ 日期：2026-08-16 ｜ 状态：设计评审中
>
> **范围**：`tests/eval` 黄金评测器新增 **LLM 评委维度**，与既有关键词判定构成「规则硬约束 + LLM 软判定」双评委；先影子模式（只记录不影响 PASS/FAIL），用分歧率数据校准后升级为门槛模式。**本期不改后端业务代码**。
>
> **关联文档**：
> - [AI对话Reflection反思机制设计方案.md](AI对话Reflection反思机制设计方案.md)（critic 提示词 / 超时 / fail-safe 模式复用）
> - [README.md](../README.md)「测试与评测」节（评测器现状口径）
> - [ReAct与Plan-Execute混合路由方案.md](ReAct与Plan-Execute混合路由方案.md)（轻量模型复用约定）

---

## 概述

当前黄金评测器的判定逻辑是**关键词子串匹配**（`expected_keywords` any/all 命中 + 无 `[ERROR:`），存在两类盲区：

1. **误杀**：回答语义正确但措辞不含关键词 → 判 FAIL（如 complex-002 期望「量子」「总结」「原理」，模型换同义词表达即不命中）；
2. **漏判**：回答答非所问但恰好包含关键词 → 判 PASS（如 gen-001「1+1等于几？只回答数字」期望 `["2"]`，模型答「2+2=4」同样含 "2" 通过）。

企业主流解法是 **LLM-as-Judge**（Ragas / DeepEval 等评测框架均以此为核）。本方案在现有 runner 上叠加 LLM 评委：复用轻量模型（DashScope qwen3-flash，与 `reflection.critic_model: light` 同档），按固定 rubric 对回答的 **faithfulness（忠实不编造）/ relevance（切题）/ completeness（完整）** 三维打分。

**为何自研而非直接引入 Ragas**（面试常问，提前论证）：

| 维度 | Ragas | 自研双评委 |
| :--- | :--- | :--- |
| 依赖 | 引入 langchain 生态全家桶，评测器从"单文件脚本"变重型工程 | 零新依赖（OpenAI 兼容 SDK 直连） |
| 与现有 runner 集成 | 需重构用例/报告结构 | 向后兼容（新增可选字段） |
| 判定口径 | 黑盒指标，难对齐项目语义（笔记/RAG/安全用例） | rubric 自定，维度可解释 |
| 简历叙事 | "用了 Ragas" | "自研双评委评测器，含分歧率校准闭环" |

---

## 一、现状回顾

### 1.1 当前判定与报告现状

| 现状 | 位置 | 后果 |
| :--- | :--- | :--- |
| 关键词判定 `keyword_hit`：any/all 子串匹配（忽略大小写） | [eval_runner.py:84-89](../tests/eval/eval_runner.py) | 仅能验证"说了关键词"，不能验证"说对了" |
| 用例结构：id / category / query / expected_keywords / match / enable_thinking / attachment / note | [golden_cases.json](../tests/eval/golden_cases.json) | 无质量维度字段 |
| 汇总 `summarize`：通过率 / 分类通过率 / 平均延迟 / 失败明细 | [eval_runner.py:242-269](../tests/eval/eval_runner.py) | 无质量分、无分歧统计 |
| 报告产物：`tests/eval/results/report_*.json` | [eval_runner.py:303-311](../tests/eval/eval_runner.py) | 报告格式需向后兼容扩展 |

### 1.2 既有设计资产盘点（本方案继承的决策）

| 来源 | 既有决策 | 本方案处理 |
| :--- | :--- | :--- |
| Reflection 方案 §3.1/3.7 | 批判模型复用轻量模型（qwen3-flash），独立超时预算（15s），解析失败一律放行（fail-safe） | **照此执行**（评委模型 / 超时 / 兜底，§3.2） |
| Reflection 方案 §1.3 | 提示词注册制：新模板必须注册，否则 KeyError | 本方案提示词独立于后端 prompts/（见 §2.2 论证） |
| 仓库 .env | `DASHSCOPE_API_KEY` 已存在，OpenAI 兼容端点可直接调用 | 评委直连 DashScope，**零后端改动** |
| 项目 fail-safe 文化 | 增强功能不降可用性 | 评委任何异常不影响原有关键词判定 |

### 1.3 技术约束（设计必须满足）

1. **eval_runner 是独立脚本**（httpx 直连后端，不 import 后端代码）：评委实现必须自包含在 `tests/eval/` 内，不引入对 `app/` 的依赖（否则失去"独立客户端"定位）。
2. **golden_cases.json 向后兼容**：新增字段全部可选，旧用例文件不报错、行为不变。
3. **报告格式向后兼容**：`failures` 口径与 `pass_rate` 计算不被新字段影响（影子模式下 24/24 结论不变）。
4. **后端限流 10/min 不作用于评委**：评委直连模型 API，但需自控 QPS（串行 + 间隔，§3.5）。
5. **视觉用例（mm-*）**：轻量模型无视觉能力，评委默认跳过 multimodal 分类（可配置，§3.3）。

---

## 二、整体架构设计

### 2.1 双评委判定链路

```
run_case（单条用例）
  ├─ SSE 获取 reply（现状不变）
  ├─ 评委 1：keyword_hit（现状不变，硬约束）
  └─ 评委 2：LLM-as-Judge（新增；judge.enabled 且未传 --skip-judge 时执行）
       ├─ build_judge_prompt(case, reply)      # judge_prompt.txt 模板渲染
       ├─ judge_reply(query, rubric, reply)    # OpenAI 兼容 SDK → DashScope 轻量模型
       │    ├─ 超时/网络异常 → judge: {status: "error"}（不影响原判定）
       │    └─ 解析 JSON（剥离 codeblock，重试 1 次）→ 失败同 error
       ├─ 产出 {pass, score(1..5), dimensions{faithfulness, relevance, completeness}, issues}
       └─ 融合（judge.mode 配置，§2.4）
            ├─ shadow（默认）：judge 结果仅记录，PASS/FAIL 仍按关键词
            └─ gate（校准后）：keyword PASS 且 judge PASS 才算 PASS；分歧标注 conflict
```

### 2.2 模块划分

| 文件 | 职责 | 复用点 |
| :--- | :--- | :--- |
| `tests/eval/llm_judge.py`（新增，约 120 行） | 纯函数 `parse_judge_response`（剥离 codeblock → json.loads → 校验字段）；`judge_reply`（SDK 调用 + 超时兜底 + 重试）；`fuse_verdicts`（shadow / gate 融合） | codeblock 剥离正则同 Reflection 方案 §2.2 |
| `tests/eval/judge_prompt.txt`（新增） | 评委提示词模板（占位符：query / rubric / reply） | — |
| `eval_runner.py`（改动） | `EvalCase` 增加 judge 字段解析；`run_case` 插入评委 2 与融合；`summarize` / `print_report` 增加 judge 统计 | 仅编排，判定逻辑不内联 |
| `golden_cases.json`（改动） | 顶层新增可选 `judge` 配置段；用例级可选 `judge_skip` / `rubric` 覆盖 | 旧文件零改动可用 |

> **论证：评委提示词为何不放后端 `prompts/`**——后端 prompts/ 是运行时资产（load_prompt 按 config/prompt.yaml 注册，供 Agent 使用）；评测器是独立客户端，不 import 后端。放 `tests/eval/judge_prompt.txt` 保持自包含。若未来需要"后端也提供 judge 端点"（如评测集成进 CI 的 service 形态），届时再迁移并注册，扩展点已预留（§六 开放问题 #4）。

### 2.3 评分与判定协议（评委输出 JSON）

```json
{
  "pass": true,
  "score": 4,
  "dimensions": {"faithfulness": 5, "relevance": 4, "completeness": 3},
  "issues": "回答完整但未说明时间复杂度"
}
```

- `faithfulness`：是否忠于用户问题与检索/笔记内容，**是否编造**（权重最高，与项目安全定位一致）；
- `relevance`：是否切题（对应现状关键词判定的"答非所问"盲区）；
- `completeness`：多要点问题是否覆盖全（对应 complex 分类用例）。
- rubric 锚点：5=完全达标，4=小瑕疵，3=部分达标，2=明显偏差，1=答非所问/编造。`pass` 判定：score ≥ 4 且 faithfulness ≥ 4。

### 2.4 融合策略与校准（影子 → 门槛）

| 阶段 | 模式 | 判定口径 | 目的 |
| :--- | :--- | :--- | :--- |
| Phase 1 | shadow | 关键词判定（现状） | 收集评委基线数据，零风险上线 |
| Phase 2 | shadow（统计期） | 同上 | 跑 ≥3 轮全量，产出**分歧率报告**：① keyword PASS ∧ judge FAIL（疑似漏判）；② keyword FAIL ∧ judge PASS（疑似误杀/用例歧义） |
| Phase 3 | gate（可配） | keyword PASS **且** judge PASS | 双评委正式门槛；`judge_skip_categories` 例外（默认 `["multimodal"]`） |

融合函数 `fuse_verdicts(keyword_pass, judge) -> {pass, conflict}`（纯函数，可单测）：

- shadow：`pass = keyword_pass`；`conflict = keyword_pass != judge.pass`（judge 有效时）
- gate：`pass = keyword_pass and judge.pass`；`conflict = keyword_pass != judge.pass`

---

## 三、详细设计

### 3.1 评委提示词模板（`tests/eval/judge_prompt.txt`）

要点（严格按 Reflection critic 的"结构化 JSON + 只输出 JSON"模式）：

1. 角色：严格的评测评委，对 AI 回答做质量判定；
2. 输入：用户问题、用例期望说明（expected_keywords 仅作**参考**，不作为判定依据）、实际回答；
3. 判定标准：三维 rubric（faithfulness / relevance / completeness）及 1–5 锚点定义；
4. 输出：只输出 JSON，不含任何解释文字（codeblock 包裹也可，解析层兼容）；
5. 特别约束：关键词是提示不是标准——回答语义正确但未含关键词应 PASS；含关键词但答非所问应 FAIL。

### 3.2 判定与失败兜底

| 场景 | 处理 |
| :--- | :--- |
| 网络异常 / 超时（judge_timeout 默认 30s） | `judge: {status: "error"}`，原判定不受影响 |
| JSON 解析失败 | 重试 1 次（重新调用）→ 仍失败 → status=error |
| 字段缺失 / 类型错误 | 视为解析失败，同上 |
| judge 总开关关闭 / 单用例 judge_skip | 该用例无 judge 字段，报告跳过 |

核心原则：**评委是增强不是关卡，任何失败一律不影响原有关键词判定**（与 Reflection fail-safe 一脉相承）。

### 3.3 配置设计（`golden_cases.json` 顶层 `judge` 段）

```json
"judge": {
  "enabled": true,
  "mode": "shadow",                    // shadow | gate
  "model": "qwen3-flash",              // 轻量模型（reflection.critic_model 同档）
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "api_key_env": "DASHSCOPE_API_KEY",  // 引用环境变量名，不落盘密钥
  "timeout": 30,
  "skip_categories": ["multimodal"]    // 轻量模型无视觉能力，跳过 mm-*
}
```

用例级可选覆盖：`"judge_skip": true`（如安全用例 sec-001 判定口径特殊）、`"rubric": "..."`（自定义评分要点）。

### 3.4 报告扩展（向后兼容）

- 单条 result 追加 `judge` 字段：`{score, pass, issues, status, conflict}`（无评委时缺省）；
- `summarize` 追加 `judge_stats`：平均分、faithfulness/relevance/completeness 分维度均值、分歧率（两类，§2.4）、judge 通过率、judge_error 数；
- `print_report` 在分类表中追加「评委均分」列，失败明细追加 judge issues（便于人工复核）；
- `failures` 列表口径不变（仍按最终 pass 判定），保证旧消费方兼容。

### 3.5 成本与预算

| 项 | 估算 |
| :--- | :--- |
| 单条评委调用 | ~300 in + ~200 out token（query+rubric+回答节选），qwen3-flash 单价极低 |
| 全量 24 条 | 约 1.2 万 token / 轮，串行 ~2s/条 + 现有 interval 7s ≈ 增 1~2 分钟 |
| 控制 | 评委串行执行（不并发），复用 runner 的 interval 节奏，不触碰 DashScope QPS 上限 |

### 3.6 CLI 扩展

```bash
tests/eval/eval_runner.py [--skip-judge] [--judge-model qwen3-flash] [--judge-mode shadow|gate]
```

`--judge-mode` 覆盖配置文件（校准实验用，不改用例文件即可切换）。

---

## 四、文件级改动清单

| 文件 | 改动 | 规模 |
| :--- | :--- | :--- |
| `tests/eval/llm_judge.py` | 新增：解析 / 调用 / 融合纯函数 | ~120 行 |
| `tests/eval/judge_prompt.txt` | 新增：评委提示词模板 | ~40 行 |
| `tests/eval/eval_runner.py` | `EvalCase` 加字段；`run_case` 插入评委 2；`summarize`/`print_report` 加统计；CLI 参数 | +60 行左右 |
| `tests/eval/golden_cases.json` | 顶层 judge 段 + 用例级可选字段 | +10 行 |
| `tests/test_llm_judge.py` | 新增：纯函数单测 | ~80 行 |

---

## 五、测试与验证方案

### 5.1 单元测试（`tests/test_llm_judge.py`，无外部依赖）

- `parse_judge_response`：正常 JSON / codeblock 包裹 / 缺字段 / 非 JSON / 空串——后三类返回 None（触发上层 error 路径）；
- `fuse_verdicts`：shadow 与 gate 两种模式 × judge PASS/FAIL/error 三态 × conflict 标注；
- 用例字段解析：旧用例文件（无 judge 段）加载不报错。

### 5.2 评委区分度验证（Phase 1 验收，构造 2 条校准用例）

| 用例 | 场景 | 预期 |
| :--- | :--- | :--- |
| judge-calib-001 | 问「1+1等于几？只回答数字」，模拟回答「2+2=4」 | 关键词 PASS（含 "2"），**评委应 FAIL**（答非所问） |
| judge-calib-002 | 问「中国的首都」，模拟回答「中华人民共和国首都是北京（首都）」 | 关键词 FAIL（设关键词 "北京" 之外）→ 用同义表述使关键词不命中，**评委应 PASS**（语义正确） |

两条用例不进入正式 24 条集（或标注 `calibration: true` 排除出通过率口径），仅用于验证评委区分度。

### 5.3 全量回归

- 影子模式跑正式 24 条：通过率结论与 2026-08-06 报告一致（24/24），报告新增 judge 列与分歧统计；
- gate 模式不默认开启（进入 Phase 2 校准后由数据决定）。

---

## 六、风险与开放问题

1. **自评偏差**：评委与被评 Agent 同源（qwen 系列）。缓解：rubric 锚点固定 + 只评语义事实性维度 + `model` 可配置换模型对照（如 qwen-max 评 flash 输出）。
2. **视觉用例**：轻量模型无视觉能力，默认跳过 multimodal；若需评多模态用例，Phase 2 可换视觉模型评委（`judge.model` 配置切换）。
3. **gate 模式误杀风险**：评委 FAIL 但关键词 PASS 的用例需人工复核是"回答真差"还是"评委过严"——这正是 Phase 2 分歧报告的用途，**未跑满 3 轮校准前不得开启 gate**。
4. **开放问题**：是否提供后端 `/internal/eval/judge` 端点（复用 plan_model + load_prompt），让 CI 里其他消费者也能调用评委？——本期不做，扩展点预留（llm_judge.py 的调用层与解析层已分离）。
5. **开放问题**：是否引入 Ragas 做第三方指标对照（faithfulness 指标对比自研 rubric）？——本期不做，可在面试中作为"为什么自研"的论证素材。

---

## 七、实施阶段

| 阶段 | 内容 | 预计 |
| :--- | :--- | :--- |
| Phase 1 | llm_judge.py + 提示词 + runner 集成（shadow 模式）+ 单测 + 校准用例验证 | 1~2 天 |
| Phase 2 | 跑 ≥3 轮全量评测，产出分歧率报告，修订 rubric 锚点 | 2~3 天（含模型调用时间） |
| Phase 3 | 开放 gate 模式（配置切换），报告与 CI 门禁更新 | 0.5 天 |

---

## 修订记录

| 版本 | 日期 | 说明 |
| :--- | :--- | :--- |
| v1.0 | 2026-08-16 | 初版：双评委架构、影子→门槛校准、自研 vs Ragas 论证 |
