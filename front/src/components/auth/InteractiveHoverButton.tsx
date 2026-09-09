/**
 * 悬停交互按钮（登录 / 注册页提交按钮）
 *
 * 规格来源：docs/auth-pages-vite-spec.html「控件」段
 * - 默认：文字水平居中
 * - 悬停：文字 translateX(48px) 并淡出，主色层淡入并显示箭头
 * - 加载 / 禁用时不挂 group，悬停不再触发变形
 *
 * 纯 CSS 实现（group / group-hover），无需 JS 状态。
 */

import { ArrowRight } from 'lucide-react';
import type { ButtonHTMLAttributes, ComponentType } from 'react';

type IconComponent = ComponentType<{ size?: number | string; className?: string }>;

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className' | 'children'> {
  /** 常态文案 */
  label: string;
  /** 加载态文案（默认沿用 label） */
  loadingLabel?: string;
  /** 加载中 */
  loading?: boolean;
  /** 悬停主色层里的图标（默认右箭头） */
  hoverIcon?: IconComponent;
}

export default function InteractiveHoverButton({
  label,
  loadingLabel,
  loading = false,
  hoverIcon: Icon = ArrowRight,
  disabled,
  ...rest
}: Props) {
  const inactive = loading || disabled;

  return (
    <button
      {...rest}
      disabled={inactive}
      className={`relative h-12 w-full overflow-hidden rounded-full border text-sm font-semibold
        transition-colors duration-200
        ${inactive
          ? 'cursor-not-allowed border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text-tertiary)]'
          : `group cursor-pointer border-[var(--color-accent)] bg-[var(--color-card)] text-[var(--color-text)]
             hover:border-[var(--color-accent)]
             focus-visible:shadow-[0_0_0_3px_var(--color-accent-bg)]`}`}
    >
      {/* 常态层：悬停时右移淡出 */}
      <span className="inline-block transition-all duration-300 group-hover:translate-x-12 group-hover:opacity-0">
        {loading ? (loadingLabel ?? label) : label}
      </span>

      {/* 主色层：悬停时淡入 */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2
          rounded-full bg-[var(--color-accent)] text-[var(--color-on-accent)]
          opacity-0 transition-opacity duration-300 group-hover:opacity-100"
      >
        {label}
        <Icon size={16} />
      </span>
    </button>
  );
}
