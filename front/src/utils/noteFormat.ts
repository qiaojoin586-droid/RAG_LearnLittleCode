/**
 * 笔记格式（文档类型功能，2026-08-21 起后端 notes.format 字段落库）
 *
 * - 新建笔记时用户可选 md（Markdown，默认）/ txt（纯文本）
 * - 列表徽章与编辑器模式按后端 format 字段渲染
 * - 存量数据 / 字段缺失 / 未知值一律兜底 'md'（兼容旧数据与异常）
 */

export type NoteFormat = 'md' | 'txt';

/** 归一化后端 format 字段：仅 'txt' 判为纯文本，其余（含缺失/未知）兜底 'md' */
export function detectNoteFormat(format?: string | null): NoteFormat {
  return format === 'txt' ? 'txt' : 'md';
}

/**
 * 生成笔记摘要：去除 Markdown 标记后截取前约 max 字（用于卡片列表第二栏）
 */
export function noteExcerpt(content: string, max = 80): string {
  const plain = content
    .replace(/```[\s\S]*?```/g, ' ') // 代码块
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ') // 图片
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // 链接保留文字
    .replace(/^#{1,6}\s+/gm, '') // 标题符号
    .replace(/[*_~`>#]/g, '') // 其余标记字符
    .replace(/\s+/g, ' ')
    .trim();
  return plain.length > max ? `${plain.slice(0, max)}…` : plain;
}
