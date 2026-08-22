/**
 * Markdown 预览渲染管线测试（v2）
 *
 * 用 renderToString 验证「每日回顾预览区 / 笔记编辑器预览」所用
 * ReactMarkdown + remarkGfm + rehypeHighlight 管线能正确转换语法：
 * 多级标题、加粗、列表、行内代码、代码块、表格（GFM）。
 */

import { renderToString } from 'react-dom/server';
import { describe, it, expect } from 'vitest';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { isHtml, normalizeNoteContent, escapeInlineHtml } from '../markdownCompat';

/** 与每日回顾预览区一致的渲染组件 */
function renderMarkdown(content: string): string {
  return renderToString(
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
      {content}
    </ReactMarkdown>
  );
}

const SAMPLE = `LLM（大语言模型）与 Agent（智能体）的核心区别在于：**LLM 是"大脑"**

## 1. 核心定位
- **LLM**：本质是一个概率预测模型
- \`token\` 是预测的基本单位

\`\`\`
const x = 1;
\`\`\`

| 维度 | LLM | Agent |
| :--- | :--- | :--- |
| 交互 | 被动响应 | 主动规划 |
`;

describe('Markdown 渲染管线', () => {
  it('标题/加粗/列表/行内代码/代码块/表格均正确转换（无原始语法残留）', () => {
    const html = renderMarkdown(SAMPLE);

    expect(html).toContain('<h2'); // ## 标题 → h2
    expect(html).toContain('<strong'); // **加粗** → strong
    expect(html).toContain('<li'); // - 列表 → li
    expect(html).toContain('<code'); // 行内代码/代码块 → code
    expect(html).toContain('<table'); // GFM 表格 → table
    // 关键断言：原始语法标记不得直接出现在输出中
    expect(html).not.toContain('## 1');
    expect(html).not.toContain('**LLM**');
  });

  it('普通段落换行正确分段（无挤压现象）', () => {
    const html = renderMarkdown('第一段。\n\n第二段。');
    expect(html).toContain('<p>第一段。</p>');
    expect(html).toContain('<p>第二段。</p>');
  });

  it('整篇被一层代码围栏包裹时剥除外层围栏（否则全文渲染为代码块）', () => {
    const wrapped = '```markdown\n## 标题\n\n正文内容\n```';
    expect(normalizeNoteContent(wrapped)).toBe('## 标题\n\n正文内容');
  });

  it('普通 Markdown 内容不受归一化影响；CRLF 转 LF', () => {
    const plain = '## 标题\r\n\r\n正文';
    expect(normalizeNoteContent(plain)).toBe('## 标题\n\n正文');
  });

  it('嵌套围栏（真实代码示例）不误剥', () => {
    const nested = '```\n```js\nconst x = 1;\n```\n```';
    expect(normalizeNoteContent(nested)).toBe(nested);
  });

  it('Java 泛型角括号不被误判为 HTML（回归：泛型符号曾被删光）', () => {
    const generics = '## 泛型类\n\n```java\npublic class Box<T> {\n  private T content;\n}\n```\n\n使用类型参数（如 <T>）实现类型安全，通配符如 <? extends T>。';
    expect(isHtml(generics)).toBe(false);
  });

  it('真实 HTML 内容仍能识别（闭合标签/属性/br）', () => {
    expect(isHtml('<p>你好</p>')).toBe(true);
    expect(isHtml('<div class="card">内容</div>')).toBe(true);
    expect(isHtml('第一行<br>第二行')).toBe(true);
  });

  it('escapeInlineHtml：围栏外转义、围栏内保持原样', () => {
    const md = '参数类型（如 <T>）\n\n```java\nList<String> list = new ArrayList<>();\n```\n\nList<String> 在运行时与 List<Integer> 相同';
    const escaped = escapeInlineHtml(md);
    // 仅转义 `<`（> 无需转义），渲染时还原为 "<"
    expect(escaped).toContain('参数类型（如 &lt;T>）');
    expect(escaped).toContain('List<String> list = new ArrayList<>();'); // 围栏内原样
    expect(escaped).toContain('List&lt;String> 在运行时');
  });

  it('端到端：Java 泛型笔记经转义后渲染，<T> 完整保留', () => {
    const md = '使用类型参数 <T> 实现类型安全。\n\n```java\npublic class Box<T> {}\n```';
    const html = renderMarkdown(escapeInlineHtml(md));
    // 围栏外的 <T> 经 &lt; 转义后渲染回 "<T>"（renderToString 输出中为 &lt;T&gt;）
    expect(html).toContain('&lt;T&gt;');
    // 围栏内代码原样保留（高亮器会把 token 拆进 span，故断言行尾片段）
    expect(html).toContain('&lt;T&gt; {}');
  });
});
