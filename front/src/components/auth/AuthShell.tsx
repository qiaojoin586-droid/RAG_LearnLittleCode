/**
 * 认证页外壳（登录 / 注册共用）
 *
 * 布局规格来源：docs/auth-pages-vite-spec.html「布局骨架」
 * - 左栏 lg 以上显示：灰色渐变底 + logo / 角色舞台 / 页脚
 * - 右栏：表单容器 max-w-[420px]，窄屏顶部补一个居中 logo
 *
 * 高度策略（对规格的两处修正）：
 * 1. 只在 lg 以上锁一屏（lg:h-screen lg:overflow-hidden）。规格的 max-h-screen 是恒等于
 *    100vh + overflow-hidden，窄屏下会把注册页 6 个字段的表单直接裁掉且无法滚动。
 * 2. 右栏用 flex-col + overflow-y-auto + 内层 m-auto，而不是 items-center——
 *    flex 的 align-items:center 在内容溢出时会裁掉起始边（顶部），滚动条也够不到。
 *
 * isTyping 由本组件持有：右栏容器的 onFocus/onBlur 走 focusin/focusout 会冒泡，
 * 一处委托即可捕获所有输入框的聚焦，页面无需给每个字段接线。
 */

import { useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { NotebookPen } from 'lucide-react';
import AnimatedCharacters from './AnimatedCharacters';

interface Props {
  /** 密码是否明文可见（驱动角色「集体转头」与「偷看」） */
  showPassword: boolean;
  /** 密码已输入长度（为 0 时角色对可见性变化无反应） */
  passwordLength: number;
  /** 右栏表单 */
  children: ReactNode;
}

export default function AuthShell({ showPassword, passwordLength, children }: Props) {
  const { t } = useTranslation();
  const [isTyping, setIsTyping] = useState(false);

  return (
    <div className="grid min-h-screen lg:h-screen lg:overflow-hidden lg:grid-cols-2">

      {/* ── 左栏：窄于 lg 整栏隐藏 ── */}
      <div
        className="relative hidden flex-col justify-between overflow-hidden p-12 lg:flex
          bg-gradient-to-br from-gray-400 via-gray-500 to-gray-600
          dark:from-white/90 dark:via-white/80 dark:to-white/70
          text-white dark:text-gray-900"
      >
        {/* 网格纹理 + 两个光斑（装饰，不参与交互） */}
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundImage:
              'linear-gradient(rgba(255,255,255,0.10) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.10) 1px, transparent 1px)',
            backgroundSize: '32px 32px',
          }}
          aria-hidden="true"
        />
        <div className="pointer-events-none absolute -left-16 top-1/4 h-72 w-72 rounded-full bg-white/30 blur-3xl" aria-hidden="true" />
        <div className="pointer-events-none absolute -right-10 bottom-10 h-64 w-64 rounded-full bg-white/20 blur-3xl" aria-hidden="true" />

        {/* 上：品牌 */}
        <div className="relative z-10 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-[var(--radius-md)] bg-white/20">
            <NotebookPen size={20} />
          </div>
          <span className="text-lg font-semibold">{t('auth.brand')}</span>
        </div>

        {/* 中：角色舞台（吃掉剩余高度，矮屏时角色自动缩小） */}
        <div className="relative z-10 flex min-h-0 flex-1 items-end justify-center py-8">
          <AnimatedCharacters
            isTyping={isTyping}
            showPassword={showPassword}
            passwordLength={passwordLength}
          />
        </div>

        {/* 下：页脚 */}
        <p className="relative z-10 text-sm opacity-80">{t('auth.panelFooter')}</p>
      </div>

      {/* ── 右栏：表单 ── */}
      <div
        className="flex flex-col overflow-y-auto bg-[var(--color-bg)] p-8"
        // 只让输入框驱动 isTyping：按钮 / 链接获得焦点时不该触发「打字」动作
        onFocus={(e) => {
          if ((e.target as HTMLElement).tagName === 'INPUT') setIsTyping(true);
        }}
        onBlur={(e) => {
          if ((e.target as HTMLElement).tagName === 'INPUT') setIsTyping(false);
        }}
      >
        <div className="m-auto w-full max-w-[420px]">
          {/* 窄屏 logo（左栏已隐藏） */}
          <div className="mb-8 flex items-center justify-center gap-3 lg:hidden">
            <div className="flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] bg-[var(--color-accent-bg)] text-[var(--color-accent)]">
              <NotebookPen size={18} />
            </div>
            <span className="text-lg font-semibold text-[var(--color-text)]">{t('auth.brand')}</span>
          </div>

          {children}
        </div>
      </div>
    </div>
  );
}
