/**
 * noteReferenceParser 单元测试（v2 引用笔记卡片化改造）
 */

import { describe, it, expect } from 'vitest';
import { parseReferencedNotes } from '../noteReferenceParser';

describe('parseReferencedNotes', () => {
  it('解析用户真实样本：问题 + 单篇引用（含 ID/标题/完整正文）', () => {
    const content = [
      '总结提炼一下这篇笔记',
      '',
      '---',
      '以下是用户引用的笔记内容，请结合这些内容回答：',
      '【笔记：Agent与LLM的区别】',
      'LLM（大语言模型）与 Agent（智能体）的核心区别在于：',
      '',
      '## 1. 核心定位',
      '- LLM：本质是概率预测模型',
      '',
      '<referenced_notes>',
      '- ID: c3326ca6-abc2-4187-8019-f118b7219ac8 | 标题: Agent与LLM的区别',
      '</referenced_notes>',
    ].join('\n');

    const block = parseReferencedNotes(content);
    expect(block).not.toBeNull();

    // 原始问题还原（去掉 --- 分隔线）
    expect(block!.prefix).toBe('总结提炼一下这篇笔记');

    expect(block!.notes).toHaveLength(1);
    expect(block!.notes[0].id).toBe('c3326ca6-abc2-4187-8019-f118b7219ac8');
    expect(block!.notes[0].title).toBe('Agent与LLM的区别');
    expect(block!.notes[0].content).toContain('## 1. 核心定位');
    expect(block!.notes[0].content).not.toContain('<referenced_notes>');

    // 元数据块与上下文段落不进 suffix
    expect(block!.suffix.trim()).toBe('');
  });

  it('多篇引用按【笔记：】切分并与 ID 对应', () => {
    const content = [
      '对比这两篇笔记',
      '',
      '---',
      '以下是用户引用的笔记内容，请结合这些内容回答：',
      '【笔记：笔记A】',
      'A的正文',
      '',
      '【笔记：笔记B】',
      'B的正文',
      '',
      '<referenced_notes>',
      '- ID: id-a | 标题: 笔记A',
      '- ID: id-b | 标题: 笔记B',
      '</referenced_notes>',
      '后续内容',
    ].join('\n');

    const block = parseReferencedNotes(content);
    expect(block).not.toBeNull();
    expect(block!.notes).toHaveLength(2);
    expect(block!.notes[0].id).toBe('id-a');
    expect(block!.notes[0].content).toBe('A的正文');
    expect(block!.notes[1].id).toBe('id-b');
    expect(block!.notes[1].content).toBe('B的正文');
    expect(block!.suffix).toContain('后续内容');
  });

  it('新卡片格式（v2 发送给 LLM 的线上格式）', () => {
    const content = [
      '总结提炼',
      '',
      '---',
      '以下是用户引用的笔记卡片：',
      '',
      '【卡片 1｜Agent与LLM的区别】',
      'LLM（大语言模型）与 Agent（智能体）的核心区别在于：',
      '',
      '【卡片 2｜RAG 入门笔记】',
      'RAG 是检索增强生成。',
      '',
      '<referenced_notes>',
      '- ID: id-1 | 标题: Agent与LLM的区别',
      '- ID: id-2 | 标题: RAG 入门笔记',
      '</referenced_notes>',
    ].join('\n');

    const block = parseReferencedNotes(content);
    expect(block).not.toBeNull();
    expect(block!.prefix).toBe('总结提炼');
    expect(block!.notes).toHaveLength(2);
    expect(block!.notes[0].id).toBe('id-1');
    expect(block!.notes[0].title).toBe('Agent与LLM的区别');
    expect(block!.notes[0].content).toContain('核心区别');
    expect(block!.notes[1].id).toBe('id-2');
    expect(block!.notes[1].content).toBe('RAG 是检索增强生成。');
  });

  it('无引用块时不解析（原样渲染）', () => {
    const content = '普通提问：今天天气如何？';
    expect(parseReferencedNotes(content)).toBeNull();
  });
});
