/**
 * 笔记格式推断工具（v2，不动后端）
 *
 * 后端 Note 类型无 format 字段，采用客户端启发式推断：
 * content 含 Markdown 语法特征 → 'md'，否则 'txt'。
 * 将来后端增加 format 字段后，仅需替换本文件的 detectNoteFormat 实现。
 */

export type NoteFormat = 'md' | 'txt';

/** Markdown 语法特征（标题/列表/代码块/链接/引用/加粗/有序列表） */
const MD_PATTERNS: RegExp[] = [
  /^#{1,6}\s+/m,
  /^[-*+]\s+/m,
  /^>\s/m,
  /```/,
  /\[[^\]]+\]\([^)]+\)/,
  /\*\*[^*]+\*\*/,
  /^\d+\.\s+/m,
];

/** 推断笔记格式 */
export function detectNoteFormat(content: string): NoteFormat {
  if (!content) return 'txt';
  return MD_PATTERNS.some((p) => p.test(content)) ? 'md' : 'txt';
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
