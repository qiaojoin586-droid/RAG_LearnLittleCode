/**
 * 顶部导航栏（v2 新壳层）
 *
 * 统一顶栏，高度 56px（h-14），毛玻璃背景：
 * - 左侧：Logo + 项目名称
 * - 居中：全局搜索入口（点击唤起全局搜索模态框，阶段四实现检索本体）
 * - 右侧：AI 助手入口按钮 + 用户头像菜单
 *
 * z-index 层级：TopBar(100) > AI 浮层(90) > 移动端遮罩(80) > SidePanel(70) > IconRail(60) > 内容区(10)
 */

import { useTranslation } from 'react-i18next';
import { Search, Sparkles } from 'lucide-react';
import UserAvatarMenu from './UserAvatarMenu';

interface TopBarProps {
  /** AI 浮层是否可见（用于按钮选中态） */
  assistantVisible: boolean;
  /** 切换 AI 浮层显隐 */
  onToggleAssistant: () => void;
  /** 唤起全局搜索 */
  onGlobalSearch: () => void;
}

export default function TopBar({ assistantVisible, onToggleAssistant, onGlobalSearch }: TopBarProps) {
  const { t } = useTranslation();

  return (
    <header
      className="h-14 flex items-center justify-between px-4 border-b border-[var(--color-topbar-border)] shrink-0 relative"
      style={{
        zIndex: 100,
        background: 'var(--color-topbar-bg)',
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
      }}
    >
      {/* 左侧：Logo + 项目名称 */}
      <div className="flex items-center gap-2 shrink-0">
        <div className="w-8 h-8 rounded-lg bg-[var(--color-accent)] flex items-center justify-center text-white font-bold text-sm select-none">
          云
        </div>
        <span className="font-heading text-base font-bold text-[var(--color-text)]">云尚</span>
      </div>

      {/* 居中：全局搜索入口（按钮形态，点击唤起模态框；移动端只显示图标） */}
      <div className="flex-1 max-w-md mx-4 sm:mx-8">
        <button
          onClick={onGlobalSearch}
          className="w-full h-9 px-3 rounded-lg text-sm flex items-center gap-2
            bg-[var(--color-search-bg)] border border-[var(--color-search-border)]
            text-[var(--color-text-tertiary)]
            hover:border-[var(--color-search-focus-border)] hover:text-[var(--color-text-secondary)]
            transition-colors duration-200"
        >
          <Search size={16} className="shrink-0" />
          <span className="truncate hidden sm:inline">{t('topbar.searchPlaceholder')}</span>
        </button>
      </div>

      {/* 右侧：AI 助手入口 + 用户菜单 */}
      <div className="flex items-center gap-3 shrink-0">
        <button
          onClick={onToggleAssistant}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium
            transition-all duration-200 ${
              assistantVisible
                ? 'text-[var(--color-on-accent)] bg-[var(--color-accent)]'
                : 'text-[var(--color-quick-action-text)] bg-[var(--color-quick-action-bg)] border border-[var(--color-quick-action-border)] hover:opacity-90'
            }`}
        >
          <Sparkles size={16} />
          <span>{t('topbar.assistant')}</span>
        </button>
        <UserAvatarMenu />
      </div>
    </header>
  );
}
