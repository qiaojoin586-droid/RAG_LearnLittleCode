/**
 * 引用笔记块解析（v2 卡片化改造：用户消息侧）
 *
 * 用户引用笔记发送时，消息组合为固定格式（useChatComposer 生成）：
 *
 *   用户原始问题
 *   ---
 *   以下是用户引用的笔记内容，请结合这些内容回答：
 *   【笔记：标题A】
 *   正文A...
 *   【笔记：标题B】
 *   正文B...
 *   <referenced_notes>
 *   - ID: xxx | 标题: 标题A
 *   </referenced_notes>
 *
 * 后端将整段原样入库（chat.py user_content = data.message），历史回放时
 * 用户气泡会显示完整文本块。本解析器在展示层将引用块拆出：
 * - prefix：用户原始问题（正常 Markdown 渲染）
 * - notes：结构化引用笔记（标题/正文/ID，正文来自消息文本，无需再请求）
 * - 原始上下文段落与 <referenced_notes> 元数据块隐藏，由卡片替代
 *
 * 解析失败返回 null，调用方原样渲染。
 */

export interface ReferencedNote {
  /** 笔记 ID（来自 <referenced_notes> 块） */
  id?: string;
  title: string;
  /** 完整正文（消息文本内携带） */
  content: string;
}

export interface ParsedReferenceBlock {
  /** 用户原始问题 */
  prefix: string;
  notes: ReferencedNote[];
  /** 引用块之后的内容（一般为空） */
  suffix: string;
}

/** 引用块起始标记（新卡片格式与旧格式共用此前缀，兼容历史消息） */
const CONTEXT_MARKER = '以下是用户引用的笔记';

/** <referenced_notes> 元数据块 */
const REF_BLOCK_RE = /<referenced_notes>\s*([\s\S]*?)\s*<\/referenced_notes>/;

/** ID 行：- ID: xxx | 标题: yyy */
const ID_LINE_RE = /ID[:：]\s*([A-Za-z0-9_-]+)\s*\|\s*标题[:：]\s*(.+)/;

/**
 * 笔记头：兼容两种格式
 * - 旧格式（历史消息）：【笔记：标题】
 * - 新卡片格式（v2）：【卡片 1｜标题】/【卡片 1 | 标题】
 */
const NOTE_HEADER_RE = /【(?:笔记|卡片(?:\s*\d+)?)\s*[｜|：:]\s*([^】]+)】/g;

export function parseReferencedNotes(content: string): ParsedReferenceBlock | null {
  const refMatch = REF_BLOCK_RE.exec(content);
  if (!refMatch) return null;

  // 解析 ID 条目（按标题与正文块对应）
  const idEntries: { id: string; title: string }[] = [];
  for (const line of refMatch[1].split('\n')) {
    const m = line.trim().match(ID_LINE_RE);
    if (m) idEntries.push({ id: m[1], title: m[2].trim() });
  }

  const contextStart = content.indexOf(CONTEXT_MARKER);
  if (contextStart < 0) return null;

  // 原始问题：去掉末尾的「---」分隔线
  const prefix = content
    .slice(0, contextStart)
    .replace(/\s*[-—–]+\s*$/, '')
    .trim();

  // 上下文段落：标记之后、<referenced_notes> 之前；按笔记头切分正文
  const contextSection = content.slice(contextStart + CONTEXT_MARKER.length, refMatch.index);

  const headers: { title: string; start: number; end: number }[] = [];
  NOTE_HEADER_RE.lastIndex = 0;
  let hm: RegExpExecArray | null;
  while ((hm = NOTE_HEADER_RE.exec(contextSection)) !== null) {
    const title = hm[1].trim();
    if (title) headers.push({ title, start: hm.index, end: hm.index + hm[0].length });
  }

  const notes: ReferencedNote[] = [];
  headers.forEach((h, idx) => {
    const body = contextSection
      .slice(h.end, idx + 1 < headers.length ? headers[idx + 1].start : contextSection.length)
      .replace(/^\n+/, '')
      .trim();
    // ID 按标题匹配，匹配不到则按顺序兜底
    const idEntry =
      idEntries.find((e) => e.title === h.title) ??
      (idEntries.length === headers.length ? idEntries[idx] : undefined);
    notes.push({ ...(idEntry ? { id: idEntry.id } : {}), title: h.title, content: body });
  });

  if (notes.length === 0) return null;

  const suffix = content.slice(refMatch.index + refMatch[0].length);

  return { prefix, notes, suffix };
}
