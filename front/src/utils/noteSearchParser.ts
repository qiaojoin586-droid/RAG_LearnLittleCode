/**
 * 笔记搜索结果块解析（v2 卡片化改造）
 *
 * 将 AI 回复中的笔记搜索列表文本块解析为结构化结果，供 SearchResultCards 卡片化渲染。
 *
 * 已观察到的真实回复格式（LLM 无严格模板，容错匹配）：
 * 1. 引导语：「我找到了5篇与agent相关的笔记：」/「已为您找到 2 篇与"xx"相关的笔记：」
 * 2. 条目三种形态：
 *    a) 编号条目：`1. 标题`（可带续行摘要，续行以缩进/空行区分）
 *    b) 圆点条目：`- 标题`
 *    c) 纯文本行：`标题 - 描述`（标题内可能含「 - 」，按最后一个「 - 」切分）
 * 3. 列表长度以引导语中的数量 N 截断（防止吞掉列表后的收尾句）
 *
 * 解析失败（无引导语/无条目）返回 null，调用方原样渲染 Markdown——
 * 0 条结果的"未找到相关笔记"等文本不受影响。
 */

export interface ParsedSearchResult {
  /** 标题（已去除加粗/编号前缀/ID 标记；纯文本行按最后「 - 」切分） */
  title: string;
  /** 正文摘要/描述（可能为空） */
  excerpt: string;
  /** 笔记 ID（消息文本携带时提取） */
  noteId?: string;
}

export interface ParsedSearchBlock {
  /** 块之前的内容（含引导文案） */
  prefix: string;
  results: ParsedSearchResult[];
  /** 块之后的内容 */
  suffix: string;
}

/**
 * 引导语（容错，已观察到的变体）：
 * - 「我找到了5篇与agent相关的笔记：」（含数量）
 * - 「已为您找到 2 篇与"xx"相关的笔记：」（含数量）
 * - 「找到以下与 Agent 相关的笔记：」（无数量）
 * 规则：动词（找到/搜索/检索/查询/搜到/查到）+ 数量「N 篇」或「以下」+ 笔记 + 行尾冒号。
 * 只含动词+笔记的普通过渡句（如"我帮你搜索了相关笔记："）不算引导语。
 */
const INTRO_RE = /^[ \t]*[^\n]*?(?:找到|搜索|检索|查询|搜到|查到)了?(?:\s*\d+\s*篇|以下)[^\n]*?笔记[^\n]*?[:：]\s*/m;

/** 编号/圆点条目前缀（`1.` / `1、` / `1)` / `-` / `*`，后随空格可选） */
const LIST_PREFIX_RE = /^(\d+[.、)]|[-*])\s*(.+)$/;

/** 标题-描述分隔符（含全半角变体），取最后一个切分以保护标题内的连字符 */
const TITLE_SEPARATORS = [' - ', ' – ', ' — ', ' − '];

/** 从条目文本中提取 ID（`ID:xxx` / `(ID: xxx)`） */
function extractNoteId(text: string): { noteId?: string; cleaned: string } {
  const idMatch = text.match(/\(?ID[:：]\s*([A-Za-z0-9_-]+)\)?/i);
  const cleaned = text
    .replace(/\(?ID[:：]\s*[A-Za-z0-9_-]+\)?/gi, '')
    .replace(/\*\*/g, '')
    .trim();
  return { noteId: idMatch?.[1], cleaned };
}

/**
 * 解析单条条目文本：标题 / 摘要 / ID
 * 优先级：`标题 - 描述` 切分 > 续行摘要 > 「标题：摘要」同行冒号
 */
function parseItemText(raw: string, continuation: string): ParsedSearchResult {
  const { noteId, cleaned } = extractNoteId(raw);

  let title = cleaned;
  let excerpt = continuation.trim();

  // 1) 「标题 - 描述」：取最后一个分隔符（标题内可能含连字符，如
  //    "AI 智能笔记系统 - 技术架构图解析 - 涉及Agent Pipeline…"）
  let sepIdx = -1;
  let sepLen = 0;
  for (const sep of TITLE_SEPARATORS) {
    const idx = cleaned.lastIndexOf(sep);
    if (idx > sepIdx) {
      sepIdx = idx;
      sepLen = sep.length;
    }
  }
  if (sepIdx > 0) {
    title = cleaned.slice(0, sepIdx).trim();
    excerpt = excerpt || cleaned.slice(sepIdx + sepLen).trim();
  } else if (!excerpt) {
    // 2) 同行动冒号：「标题：摘要」
    const colonIdx = cleaned.search(/[:：]/);
    if (colonIdx > 0) {
      excerpt = cleaned.slice(colonIdx + 1).trim();
      title = cleaned.slice(0, colonIdx).trim();
    }
  }

  return { title, excerpt, ...(noteId ? { noteId } : {}) };
}

export function parseSearchResults(content: string): ParsedSearchBlock | null {
  const introMatch = INTRO_RE.exec(content);
  if (!introMatch) return null;

  // 列表长度上限：引导语含「N 篇」时按 N 截断；无数量时放宽至 20，
  // 并收紧纯文本行的条目判定（见下）以保护列表后的收尾句
  const countMatch = introMatch[0].match(/(\d+)\s*篇/);
  const hasCount = countMatch !== null;
  const maxItems = hasCount ? Math.min(parseInt(countMatch![1], 10), 20) : 20;

  const rest = content.slice(introMatch.index + introMatch[0].length);
  const lines = rest.split(/\r?\n/);

  const results: ParsedSearchResult[] = [];
  let i = 0;

  // 引导语后的空行跳过（不影响 prefix/suffix 归属）
  while (i < lines.length && !lines[i].trim()) i++;

  while (i < lines.length && results.length < maxItems) {
    const trimmed = lines[i].trim();
    if (!trimmed) break; // 空行：列表结束

    const prefixMatch = trimmed.match(LIST_PREFIX_RE);
    if (prefixMatch) {
      // 编号/圆点条目：支持续行摘要（直到下一个条目前缀或空行）
      let j = i + 1;
      const excerptLines: string[] = [];
      while (j < lines.length) {
        const next = lines[j].trim();
        if (!next) break;
        if (LIST_PREFIX_RE.test(next)) break;
        excerptLines.push(next);
        j++;
      }
      const item = parseItemText(prefixMatch[2], excerptLines.join(' '));
      if (item.title) results.push(item);
      i = j;
    } else {
      // 纯文本条目：整行一条（标题 - 描述）。
      // 无数量引导语时，仅接受含「标题 - 描述」分隔符的行，
      // 防止把列表后的收尾句（如"需要查看某篇笔记的完整内容吗？"）吞成条目
      const hasSeparator = TITLE_SEPARATORS.some((s) => trimmed.includes(s));
      if (!hasSeparator && !hasCount) break;
      const item = parseItemText(trimmed, '');
      if (item.title) results.push(item);
      i++;
    }
  }

  if (results.length === 0) return null;

  const prefix = content.slice(0, introMatch.index) + introMatch[0];
  const suffix = lines.slice(i).join('\n');

  return { prefix, results, suffix };
}
