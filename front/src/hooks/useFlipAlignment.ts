/**
 * 弹出面板视口边界检测（flip 对齐逻辑）
 *
 * 适用于 absolute 锚定（bottom-full）的悬浮面板（NotePicker / TemplatePicker）：
 * 默认左对齐锚点（left-0）。若按左对齐时面板右边界超出视口
 * （锚点左缘 + 面板宽度 > window.innerWidth，预留 8px 边距），
 * 自动翻转为右对齐（right-0），保证面板完整可见。
 *
 * open 变为 true 时计算一次，并监听窗口 resize 实时修正。
 */

import { useEffect, useState } from 'react';

export function useFlipAlignment(
  open: boolean,
  panelRef: React.RefObject<HTMLDivElement | null>
): boolean {
  const [alignRight, setAlignRight] = useState(false);

  useEffect(() => {
    if (!open) return;
    const compute = () => {
      // 锚点 = 面板的定位父容器（relative 容器）
      const anchor = panelRef.current?.parentElement;
      if (!anchor) return;
      const anchorRect = anchor.getBoundingClientRect();
      // 面板实际宽度（首次渲染后测量；未测得按设计宽度 288px 兜底）
      const panelWidth = panelRef.current?.offsetWidth ?? 288;
      setAlignRight(anchorRect.left + panelWidth + 8 > window.innerWidth);
    };
    compute();
    window.addEventListener('resize', compute);
    return () => window.removeEventListener('resize', compute);
  }, [open, panelRef]);

  return alignRight;
}
