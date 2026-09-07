/**
 * 双层文本域：透明 textarea + 底层 highlighter，在光标处**内联**渲染灰色补全建议。
 *
 * 设计文档：docs/AI联机补全功能设计优化方案.md §4.3（方案 A：覆盖层）
 *
 * 原理：
 * - 光标**之前**的文本在两层中排版完全一致 → caret 位置天然精确，无需像素测量
 * - ghost text 在底层撑开后续文字（不会遮挡正文），光标**之后**的文本被推移，
 *   而上层 textarea 的文字是透明的，用户看不到差异
 *
 * 关键处理：
 * - 两层共用 `.gta-typography`，等宽字体由 `.gta-mono` 修饰类控制（F1）
 * - IME 组合期间切回「textarea 可见 + highlighter 隐藏」，否则拼音预编辑串会一起透明
 * - `enabled=false` 退回原生 textarea（零风险降级开关，§4.3.5）
 *
 * ⚠️ 传给本组件的 `className` 只能含**布局类**（flex-1 / w-full / min-h-0），
 * 不得含 p-* / text-* / leading-* / font-* —— 排版属性必须由 `.gta-typography` 统一提供。
 */

import { useCallback, useRef, useState } from 'react';

import '../styles/ghost-textarea.css';

interface GhostTextAreaProps {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  /** 补全建议文本（空串 = 不显示） */
  completion: string;
  /** 请求中（在光标处显示闪烁 …） */
  isLoading: boolean;
  /** completion 对应的完整内容光标偏移（建议插入点） */
  ghostAt: number;
  /** 是否等宽字体（md 模式 true，txt 模式 false） */
  mono?: boolean;
  /** 是否启用双层渲染；false → 退回原生 textarea */
  enabled?: boolean;
  placeholder?: string;
  /** 仅布局类，见文件头注释 */
  className?: string;
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  /** 点击 / 失焦 / 方向键 / IME 组合开始 → 丢弃建议 */
  onInteract?: () => void;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
}

const NAV_KEYS = [
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'Home', 'End', 'PageUp', 'PageDown',
];

export function GhostTextArea({
  value, onChange, completion, isLoading, ghostAt,
  mono = true, enabled = true, placeholder, className, onKeyDown, onInteract, textareaRef,
}: GhostTextAreaProps) {
  const highlighterRef = useRef<HTMLDivElement>(null);
  const [isComposing, setIsComposing] = useState(false);

  // 滚动同步：textarea 是驱动方，highlighter 被动跟随
  const handleScroll = useCallback(() => {
    const ta = textareaRef.current;
    const hl = highlighterRef.current;
    if (!ta || !hl) return;
    hl.scrollTop = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
  }, [textareaRef]);

  // ★ F2：降级为原生 textarea（移动端 / txt 模式 / 功能关闭）
  // ⚠️ 必须放在全部 Hook 调用之后：条件早退会让 Hook 调用数随 enabled 变化，
  //    违反 Rules of Hooks（react-hooks/rules-of-hooks 为 error 级）。
  // 降级路径复刻改造前两处 textarea 的样式差异：txt 不带等宽字体、md 带。
  if (!enabled) {
    return (
      <textarea
        ref={textareaRef}
        value={value}
        onChange={onChange}
        onKeyDown={onKeyDown}
        onBlur={onInteract}
        placeholder={placeholder}
        spellCheck={false}
        className={`${className ?? ''} w-full min-h-0 resize-none p-4 text-sm leading-relaxed text-[var(--color-text)] bg-transparent outline-none ${
          mono ? 'font-[var(--font-mono)]' : ''
        }`}
      />
    );
  }

  // ghost 可见时才切分文本，避免每次输入都做两次 slice
  const showGhost = !isComposing && (completion.length > 0 || isLoading);
  const before = showGhost ? value.slice(0, ghostAt) : value;
  const after = showGhost ? value.slice(ghostAt) : '';

  return (
    <div className={`relative ${className ?? ''} ${isComposing ? 'gta-composing' : ''}`}>
      {/* ① 底层：负责显示（正文 + 灰色建议 + 正文） */}
      <div
        ref={highlighterRef}
        aria-hidden="true"
        className={`gta-typography gta-highlighter ${mono ? 'gta-mono' : ''}`}
      >
        {before}
        {/* ⚠️ 必须用 showGhost 门控：否则 IME 组合期（highlighter 被隐藏）仍会渲染
            ghost span，且因未切分而落在文末——视觉上被隐藏掩盖，语义上是错的 */}
        {showGhost && completion && <span className="gta-ghost">{completion}</span>}
        {showGhost && isLoading && !completion && <span className="gta-ghost animate-pulse">…</span>}
        {after}
        {/* 尾行塌陷修复：div 会吞掉末尾换行，textarea 不会 */}
        {'\n'}
      </div>

      {/* ② 上层：负责交互（文字透明，仅 caret 与选区可见） */}
      <textarea
        ref={textareaRef}
        value={value}
        onChange={onChange}
        onKeyDown={onKeyDown}
        onScroll={handleScroll}
        onClick={onInteract}
        onBlur={onInteract}
        onKeyUp={(e) => {
          if (NAV_KEYS.includes(e.key)) onInteract?.();
        }}
        onCompositionStart={() => {
          setIsComposing(true);
          onInteract?.();   // 组合开始即丢弃建议，避免切层时残留
        }}
        onCompositionEnd={() => setIsComposing(false)}
        placeholder={placeholder}
        spellCheck={false}
        className={`gta-typography gta-textarea ${mono ? 'gta-mono' : ''}`}
      />
    </div>
  );
}
