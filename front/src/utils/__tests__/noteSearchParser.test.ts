/**
 * noteSearchParser 单元测试（v2 笔记搜索结果卡片化改造）
 */

import { describe, it, expect } from 'vitest';
import { parseSearchResults } from '../noteSearchParser';

describe('parseSearchResults', () => {
  it('解析标准引导语 + 多条目列表', () => {
    const content = [
      '我帮你搜索了相关笔记：',
      '已为您找到 2 篇与"深度学习"相关的笔记：',
      '1. **Transformer 架构**',
      '   Transformer 是一种基于自注意力机制的模型结构，发表于 2017 年...',
      '2. 反向传播推导',
      '   反向传播是训练神经网络的核心算法...',
      '',
      '希望以上内容对你有帮助。',
    ].join('\n');

    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results).toHaveLength(2);
    expect(block!.results[0].title).toBe('Transformer 架构');
    expect(block!.results[0].excerpt).toContain('自注意力机制');
    expect(block!.results[1].title).toBe('反向传播推导');
    // 引导语保留在 prefix，列表后内容进入 suffix
    expect(block!.prefix).toContain('已为您找到 2 篇');
    expect(block!.prefix).toContain('我帮你搜索了相关笔记');
    expect(block!.suffix).toContain('希望以上内容对你有帮助');
  });

  it('单条结果解析为单张卡片数据', () => {
    const content = '已为您找到 1 篇与"RAG"相关的笔记：\n1. RAG 入门笔记\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results).toHaveLength(1);
    expect(block!.results[0].title).toBe('RAG 入门笔记');
  });

  it('提取条目中的笔记 ID（含括号形式）', () => {
    const content = '已为您找到 2 篇与"测试"相关的笔记：\n1. 笔记A (ID: abc-123)\n2. 笔记B ID:xyz-9\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results[0].noteId).toBe('abc-123');
    expect(block!.results[0].title).toBe('笔记A');
    expect(block!.results[1].noteId).toBe('xyz-9');
  });

  it('「标题：摘要」同行情景', () => {
    const content = '已为您找到 1 篇与"测试"相关的笔记：\n1. 笔记C：这是同一行上的摘要\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results[0].title).toBe('笔记C');
    expect(block!.results[0].excerpt).toBe('这是同一行上的摘要');
  });

  it('无引导语时不解析（原样渲染）', () => {
    const content = '1. 普通列表\n2. 另一条\n';
    expect(parseSearchResults(content)).toBeNull();
  });

  it('0 条结果提示文本不受影响', () => {
    const content = '未找到相关笔记';
    expect(parseSearchResults(content)).toBeNull();
  });

  it('支持编号变体（1、/1)）', () => {
    const content = '已为您找到 2 篇与"测试"相关的笔记：\n1、笔记甲\n2) 笔记乙\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results[0].title).toBe('笔记甲');
    expect(block!.results[1].title).toBe('笔记乙');
  });

  it('列表后紧跟的段落（无空行）不吞入摘要', () => {
    const content = '已为您找到 1 篇与"测试"相关的笔记：\n1. 笔记丁\n\n以上是全部结果。\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results[0].excerpt).toBe('');
    expect(block!.suffix).toContain('以上是全部结果');
  });

  it('真实回复格式：纯文本行条目（标题 - 描述）+ 数量截断保护收尾句', () => {
    const content = [
      '我来帮你搜索有关agent的笔记。',
      '',
      '我找到了5篇与agent相关的笔记：',
      '',
      'AI Agent 领域最新进展（2026年8月） - 讨论行业趋势和企业应用现状',
      'Agent与LLM的区别 - 解释LLM和Agent的核心差异',
      'AI 智能笔记系统 - 技术架构图解析 - 涉及Agent Pipeline等技术架构',
      'ChatGPT 最新研究动态整理（2024-2026） - 包含相关模型更新信息',
      '机器学习学习笔记整合与学习路线规划 - 对比了不同AI技术的学习内容',
      '需要查看某篇笔记的完整内容吗？',
    ].join('\n');

    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results).toHaveLength(5);

    // 标题内含「 - 」时按最后一个分隔符切分，保护标题完整性
    expect(block!.results[0].title).toBe('AI Agent 领域最新进展（2026年8月）');
    expect(block!.results[0].excerpt).toBe('讨论行业趋势和企业应用现状');
    expect(block!.results[2].title).toBe('AI 智能笔记系统 - 技术架构图解析');
    expect(block!.results[2].excerpt).toBe('涉及Agent Pipeline等技术架构');

    // 引导语中的数量 N=5 截断：收尾句进入 suffix
    expect(block!.suffix).toContain('需要查看某篇笔记的完整内容吗？');
    expect(block!.prefix).toContain('我来帮你搜索有关agent的笔记');
  });

  it('圆点列表条目', () => {
    const content = '已为您找到 2 篇与"测试"相关的笔记：\n- 笔记戊\n- 笔记己\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results[0].title).toBe('笔记戊');
    expect(block!.results[1].title).toBe('笔记己');
  });

  it('纯文本行无分隔符：整行为标题', () => {
    const content = '我找到了1篇与测试相关的笔记：\n没有描述的标题行\n';
    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results[0].title).toBe('没有描述的标题行');
    expect(block!.results[0].excerpt).toBe('');
  });

  it('无数量引导语「找到以下与 X 相关的笔记：」+ 纯文本行条目', () => {
    const content = [
      '我来搜索与 Agent 相关的笔记。',
      '',
      '找到以下与 Agent 相关的笔记：',
      '',
      'AI Agent 领域最新进展（2026年8月） - 介绍行业趋势和企业应用现状',
      'Agent与LLM的区别 - 核心概念对比（你刚才引用的）',
      '机器学习学习笔记整合与学习路线规划 - 包含 AI Agent 相关内容',
      'ChatGPT 最新研究动态整理（2024-2026） - 涉及 Agent 相关技术',
      'DeepSeek 最新进展整理（2026年8月） - 模型进展，可能涉及 Agent 能力',
    ].join('\n');

    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results).toHaveLength(5);
    expect(block!.results[0].title).toBe('AI Agent 领域最新进展（2026年8月）');
    expect(block!.results[1].title).toBe('Agent与LLM的区别');
    expect(block!.results[4].title).toBe('DeepSeek 最新进展整理（2026年8月）');
  });

  it('无数量引导语：无分隔符的收尾句不被吞成条目', () => {
    const content = [
      '找到以下与测试相关的笔记：',
      '',
      '笔记甲 - 描述甲',
      '笔记乙 - 描述乙',
      '需要查看某篇笔记的完整内容吗？',
    ].join('\n');

    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results).toHaveLength(2);
    expect(block!.suffix).toContain('需要查看某篇笔记的完整内容吗？');
  });

  it('加固：摘要续行夹在列表中间时并入上一条目，不再残留编号列表', () => {
    const content = [
      '找到以下与测试相关的笔记：',
      '',
      '笔记甲 - 描述甲',
      '笔记乙 - 描述乙',
      '额外的摘要续行内容',
      '2. 笔记丙 - 描述丙',
      '3. 笔记丁 - 描述丁',
    ].join('\n');

    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    expect(block!.results).toHaveLength(4);
    expect(block!.results[0].title).toBe('笔记甲');
    // 续行并入上一条目的摘要
    expect(block!.results[1].title).toBe('笔记乙');
    expect(block!.results[1].excerpt).toContain('额外的摘要续行内容');
    expect(block!.results[2].title).toBe('笔记丙');
    expect(block!.results[3].title).toBe('笔记丁');
    // 编号列表不再残留在 suffix
    expect(block!.suffix.trim()).toBe('');
  });

  it('真实回复（用户提供）：条目间空行 + 分类/核心内容元数据 → 全量卡片，收尾句进 suffix', () => {
    const content = [
      '找到了几篇和 Agent 相关的笔记，内容如下：',
      '',
      '1. AI Agent 领域最新进展',
      '分类：技术调研 | 标签：AI Agent, 行业动态, LangChain',
      '核心内容：',
      '行业正进入 "Agentic Era"，企业应用快速内置任务型 AI Agent',
      '提及 Google Gemini、Moonshot Kimi、Anthropic Claude 等主流产品的 Agent 能力更新',
      'LangChain/LangGraph 成为主流开发框架生态',
      '涉及安全治理挑战与开发者建议',
      '注：原笔记标题或内容中若包含"2026年8月"等未来时间戳，可能为数据录入错误或预测性内容，请核实实际时效性。',
      '',
      '2. Agent与LLM的区别',
      '分类：技术 | 标签：AI, LLM, Agent',
      '核心内容：',
      'LLM 是"大脑"（概率预测模型），Agent 是"完整的人"（具备感知、规划、记忆和工具使用能力）',
      '关键差异：交互模式、工具使用、记忆机制、环境感知、目标导向',
      '应用场景：LLM 适合单点任务，Agent 适合端到端任务流',
      '',
      '需要我获取某篇笔记的完整内容吗？',
    ].join('\n');

    const block = parseSearchResults(content);
    expect(block).not.toBeNull();
    // 条目间空行不再中断列表：两条全部解析为卡片
    expect(block!.results).toHaveLength(2);
    expect(block!.results[0].title).toBe('AI Agent 领域最新进展');
    expect(block!.results[1].title).toBe('Agent与LLM的区别');
    // 收尾句进 suffix
    expect(block!.suffix).toContain('需要我获取某篇笔记的完整内容吗？');
  });

  it('畸形输出（首条无编号无标题，直接是原始字段）：安全降级为原文，不产生标题错位的假卡片', () => {
    const content = [
      '找到以下与 Agent 相关的笔记：',
      '',
      '分类',
      '技术调研 | 标签：AI Agent, 2026, 行业动态, LangChain',
      '',
      '核心内容',
      '2026 被称为 "Agentic Era"，企业应用正快速内置任务型 AI Agent',
      '2. Agent与LLM的区别',
      '分类：技术 | 标签：AI, LLM, Agent',
    ].join('\n');

    // 首条标题缺失，无法可靠重建 → 整体不卡片化（原样渲染）
    expect(parseSearchResults(content)).toBeNull();
  });
});
