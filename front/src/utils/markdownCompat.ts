/**
 * Markdown 兼容工具
 *
 * 旧数据兼容：部分历史笔记内容以 HTML 形式存储，展示前需转换为 Markdown。
 * 从 NoteEditor.tsx 抽取（原为页面内局部函数），供只读预览等多处复用。
 */

/**
 * 检测内容是否为 HTML 格式（v2 修正：泛型角括号不再误判）
 *
 * 旧实现 /<[a-z][\s\S]*>/i 会把 Markdown 里的泛型符号（`<T>`、`<String>`、
 * `<? extends T>`）误判为 HTML，随后 htmlToMarkdown 的标签清除会把泛型
 * 符号删光（数据损坏风险）。
 *
 * 仅以下强信号判定为 HTML（真实 HTML 必然满足其一，泛型符号均不满足）：
 * 1. 闭合标签：</tag>（泛型不会出现斜杠闭合）
 * 2. 带属性的开标签：<tag attr="...">（含等号）
 * 3. <br> 系（换行标签，Markdown 笔记中常见的显式 HTML）
 */
export const isHtml = (str: string): boolean =>
  /<\/[a-z][a-z0-9-]*\s*>/i.test(str) ||
  /<[a-z][a-z0-9-]*\s+[^>]*=[^>]*>/i.test(str) ||
  /<br\s*\/?\s*>/i.test(str);

/**
 * 转义行内 HTML（代码围栏外）
 *
 * ReactMarkdown 默认（未启用 rehype-raw）会把 `<T>` 等角括号序列解析为
 * 原始 HTML 节点并在输出中丢弃，导致泛型符号在预览中消失。
 * 将代码围栏外的 `<` 转义为 &lt;（渲染时还原显示为 "<"）；围栏内保持原样
 * （代码块按字面展示，无需转义）。
 */
export const escapeInlineHtml = (md: string): string => {
  const lines = md.split('\n');
  let inFence = false;
  return lines
    .map((line) => {
      if (/^\s*```/.test(line)) {
        inFence = !inFence;
        return line;
      }
      return inFence ? line : line.replace(/</g, '&lt;');
    })
    .join('\n');
};

/**
 * 归一化笔记内容：
 * - CRLF → LF
 * - 若整篇内容被一层 Markdown 代码围栏包裹（AI 创建/更新笔记时常见：模型
 *   把正文整体写入 ```...``` 内），剥除外层围栏还原正文——否则 ReactMarkdown
 *   会把全文渲染为单个代码块，语法标记裸露、无高亮、无段落结构
 */
export const normalizeNoteContent = (raw: string): string => {
  let text = raw.replace(/\r\n/g, '\n');
  const trimmed = text.trim();
  const fenceMatch = trimmed.match(/^```[^\n]*\n([\s\S]*?)\n```$/);
  if (fenceMatch) {
    const inner = fenceMatch[1];
    // 内层若仍是整层围栏则不剥（保留真正的代码示例场景）
    if (!/^```[\s\S]*```$/.test(inner.trim())) {
      text = inner;
    }
  }
  return text;
};

/** HTML 转 Markdown（简易转换，用于兼容旧数据） */
export const htmlToMarkdown = (html: string): string => {
  let md = html;
  md = md.replace(/<h1[^>]*>(.*?)<\/h1>/gi, '# $1\n');
  md = md.replace(/<h2[^>]*>(.*?)<\/h2>/gi, '## $1\n');
  md = md.replace(/<h3[^>]*>(.*?)<\/h3>/gi, '### $1\n');
  md = md.replace(/<(strong|b)[^>]*>(.*?)<\/\1>/gi, '**$2**');
  md = md.replace(/<(em|i)[^>]*>(.*?)<\/\1>/gi, '*$2*');
  md = md.replace(/<s[^>]*>(.*?)<\/s>/gi, '~~$1~~');
  md = md.replace(/<code[^>]*>(.*?)<\/code>/gi, '`$1`');
  md = md.replace(/<pre[^>]*><code[^>]*>([\s\S]*?)<\/code><\/pre>/gi, '```\n$1\n```\n');
  md = md.replace(/<blockquote[^>]*>([\s\S]*?)<\/blockquote>/gi, (_, inner) => {
    return inner.replace(/<[^>]*>/g, '').split('\n').map((l: string) => `> ${l}`).join('\n') + '\n';
  });
  md = md.replace(/<ul[^>]*>([\s\S]*?)<\/ul>/gi, (_, inner) => {
    return inner.replace(/<li[^>]*>([\s\S]*?)<\/li>/gi, '- $1\n').trim() + '\n';
  });
  md = md.replace(/<ol[^>]*>([\s\S]*?)<\/ol>/gi, (_, inner) => {
    let idx = 0;
    return inner.replace(/<li[^>]*>([\s\S]*?)<\/li>/gi, (_match: string, p1: string) => `${++idx}. ${p1}\n`).trim() + '\n';
  });
  md = md.replace(/<a[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/gi, '[$2]($1)');
  md = md.replace(/<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*\/?>/gi, '![$2]($1)');
  md = md.replace(/<hr[^>]*\/?>/gi, '\n---\n');
  md = md.replace(/<p[^>]*>([\s\S]*?)<\/p>/gi, '$1\n\n');
  md = md.replace(/<br[^>]*\/?>/gi, '\n');
  md = md.replace(/<[^>]+>/g, '');
  md = md.replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  md = md.replace(/\n{3,}/g, '\n\n');
  return md.trim();
};
