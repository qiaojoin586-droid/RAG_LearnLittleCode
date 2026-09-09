/**
 * 窄图标导航栏（v2 新壳层）
 *
 * 固定 56px 宽（w-14），仅显示图标，文字通过 title 提示。
 * 继承旧 Sidebar 的导航配置、回收站角标与登出逻辑。
 *
 * v2 变更：
 * - 移除 Logo 区域（已移至 TopBar）
 * - AI 对话 / 对话历史图标不导航，直接打开 AI 浮层对应视图（浮层是唯一 AI 入口）
 * - 个人信息入口迁移至 TopBar 用户菜单，底部仅保留设置 / 关于 / 登出
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  FileText, MessageSquare, BookOpen, Library,
  Settings, Info, LogOut, Trash2, Presentation, Coins,
} from 'lucide-react';
import { useUserStore } from '../../stores/useUserStore';
import { authApi } from '../../api/auth';
import { notesApi } from '../../api/notes';

/** 面板类型（与 MainLayout 保持一致） */
export type PanelType = 'notes' | 'review' | 'knowledge' | 'ppt' | 'recycle-bin';

interface IconRailProps {
  activePanel: PanelType | null;
  onPanelChange: (panel: PanelType) => void;
  /** 切换 AI 浮层：同视图再次点击关闭，否则打开对应视图（用户需求：点击开、再点关） */
  onToggleAssistant: (view: 'chat' | 'history') => void;
}

/** 主导航项配置（AI 类不导航，直接打开浮层） */
interface NavItem {
  key: string;
  to?: string;
  panelType: PanelType | null;
  icon: React.ElementType;
  labelKey: string;
  /** AI 类导航：点击打开浮层对应视图 */
  aiView?: 'chat' | 'history';
}

const navItems: NavItem[] = [
  { key: 'notes', to: '/notes', panelType: 'notes', icon: FileText, labelKey: 'nav.notes' },
  { key: 'chat', panelType: null, icon: MessageSquare, labelKey: 'nav.chat', aiView: 'chat' },
  // 「对话历史」入口已按需求移除：历史会话功能仅在 AI 浮层内部保留
  //（AIAssistantPanel 头部 History 视图切换按钮，功能不变）
  { key: 'review', to: '/review', panelType: 'review', icon: BookOpen, labelKey: 'nav.review' },
  { key: 'knowledge', to: '/knowledge', panelType: 'knowledge', icon: Library, labelKey: 'nav.knowledge' },
  { key: 'ppt', to: '/ppt-templates', panelType: 'ppt', icon: Presentation, labelKey: 'nav.ppt_templates' },
  { key: 'recycle', to: '/recycle-bin', panelType: 'recycle-bin', icon: Trash2, labelKey: 'nav.recycle_bin' },
];

const settingItems = [
  { to: '/usage', icon: Coins, labelKey: 'nav.usage' },
  { to: '/settings', icon: Settings, labelKey: 'nav.settings' },
  { to: '/about', icon: Info, labelKey: 'nav.about' },
];

export default function IconRail({ activePanel, onPanelChange, onToggleAssistant }: IconRailProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const logout = useUserStore((s) => s.logout);

  /** 回收站角标：已删除笔记数量（通过列表接口 total 获取，与旧 Sidebar 一致） */
  const [trashCount, setTrashCount] = useState(0);

  useEffect(() => {
    notesApi
      .listDeleted({ page_size: 1 })
      .then((res) => setTrashCount(res.data.data?.total ?? 0))
      .catch(() => {
        // 加载失败不阻塞导航渲染
      });
  }, []);

  /** 处理登出（传入 device_id 精确撤销当前设备会话） */
  const handleLogout = async () => {
    try {
      const deviceId = localStorage.getItem('device_id') || undefined;
      await authApi.logout(deviceId);
    } catch {
      // 即使后端失败也清除本地状态
    }
    logout();
    navigate('/login');
  };

  /** 渲染单个图标按钮 */
  const renderIconButton = (item: {
    icon: React.ElementType;
    labelKey: string;
    active?: boolean;
    showBadge?: boolean;
    onClick: () => void;
  }) => {
    const Icon = item.icon;
    return (
      <button
        key={item.labelKey}
        onClick={item.onClick}
        title={t(item.labelKey)}
        className={`relative w-10 h-10 rounded-lg flex items-center justify-center transition-all duration-200 ${
          item.active
            ? 'bg-[var(--color-accent)] text-[var(--color-on-accent)] shadow-[var(--shadow-accent-sm)]'
            : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
        }`}
      >
        <Icon size={20} />
        {item.showBadge && (
          <span className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-red-500 text-white text-[10px] leading-4 text-center">
            {trashCount > 99 ? '99+' : trashCount}
          </span>
        )}
      </button>
    );
  };

  return (
    <aside
      className="flex flex-col h-full w-14 border-r border-[var(--color-panel-border)]"
      style={{
        background: 'var(--color-sidebar-bg)',
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
      }}
    >
      {/* 主导航（从顶部开始，无 Logo 占位） */}
      <nav className="flex-1 flex flex-col items-center py-3 gap-1 overflow-y-auto">
        {navItems.map((item) =>
          renderIconButton({
            icon: item.icon,
            labelKey: item.labelKey,
            active: item.panelType !== null && item.panelType === activePanel,
            showBadge: item.key === 'recycle' && trashCount > 0,
            onClick: () => {
              if (item.aiView) {
                onToggleAssistant(item.aiView);
              } else if (item.panelType && item.to) {
                onPanelChange(item.panelType);
              }
            },
          })
        )}
      </nav>

      {/* 底部：设置 / 关于 / 登出（个人信息入口已迁移至 TopBar 用户菜单） */}
      <div className="flex flex-col items-center py-3 gap-1 border-t border-[var(--color-panel-border)]">
        {settingItems.map((item) =>
          renderIconButton({
            icon: item.icon,
            labelKey: item.labelKey,
            onClick: () => navigate(item.to),
          })
        )}
        {renderIconButton({
          icon: LogOut,
          labelKey: 'nav.logout',
          onClick: handleLogout,
        })}
      </div>
    </aside>
  );
}
