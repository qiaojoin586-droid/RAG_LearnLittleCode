/**
 * AI 消息内容渲染：Markdown + LaTeX 数学公式（KaTeX）
 * - remarkMath 解析 $...$ 行内公式与 $$...$$ 块级公式
 * - rehypeKatex 将公式渲染为 KaTeX 排版
 * - 流式中间态（未闭合的公式/代码块）由 react-markdown 安全降级为纯文本
 *
 * v2 卡片化改造：消息中的「已为您找到 N 篇…相关的笔记：+ 有序列表」文本块
 * 由 noteSearchParser 解析后替换为 SearchResultCards 卡片渲染；
 * 解析失败（含 0 条结果的"未找到相关笔记"）保持原样 Markdown 输出。
 *
 * 审查 ⑤：从 AIChat.tsx（1088 行巨型组件）拆出。
 */

import { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import SearchResultCards from './SearchResultCards';
import ReferencedNoteCards from './ReferencedNoteCards';
import { parseSearchResults } from '../../utils/noteSearchParser';
import { parseReferencedNotes } from '../../utils/noteReferenceParser';

/** Markdown 片段渲染（prefix/suffix 与整段内容共用同一套插件配置） */
function MarkdownBlock({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeHighlight, rehypeKatex]}
    >
      {content}
    </ReactMarkdown>
  );
}

export default function MessageContent({ content }: { content: string }) {
  // 用户消息：引用笔记块（优先解析——引用块仅出现在用户消息，AI 回复不含该标记）
  const referenceBlock = useMemo(() => parseReferencedNotes(content), [content]);
  // AI 消息：搜索结果块（流式逐 token 重解析，代价可忽略；完整时自动切换为卡片）
  const searchBlock = useMemo(
    () => (referenceBlock ? null : parseSearchResults(content)),
    [content, referenceBlock]
  );

  // 引用笔记卡片化：原始问题 + 引用笔记卡片（隐藏上下文段落与 <referenced_notes> 元数据块）
  if (referenceBlock) {
    return (
      <div className="md-prose">
        {referenceBlock.prefix.trim() && <MarkdownBlock content={referenceBlock.prefix} />}
        <ReferencedNoteCards notes={referenceBlock.notes} />
        {referenceBlock.suffix.trim() && <MarkdownBlock content={referenceBlock.suffix} />}
      </div>
    );
  }

  if (!searchBlock) {
    return (
      <div className="md-prose">
        <MarkdownBlock content={content} />
      </div>
    );
  }

  return (
    <div className="md-prose">
      {searchBlock.prefix.trim() && <MarkdownBlock content={searchBlock.prefix} />}
      {/* 笔记搜索结果卡片化：单条一张卡片，点击弹出详情预览 */}
      <SearchResultCards results={searchBlock.results} />
      {searchBlock.suffix.trim() && <MarkdownBlock content={searchBlock.suffix} />}
    </div>
  );
}
