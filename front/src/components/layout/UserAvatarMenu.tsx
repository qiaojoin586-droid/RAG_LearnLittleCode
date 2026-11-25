/**
 * 用户头像下拉菜单（v2 新增）
 *
 * 头像取 useUserStore.userInfo.avatar（URL）；无头像时显示用户名首字符占位。
 * 菜单项：个人信息 / 设置 / 关于 / 退出登录。
 *
 * 个人信息入口从旧 Sidebar 迁移至此（IconRail 底部不再保留 profile 项）；
 * 登出逻辑与旧 Sidebar 一致：传入 device_id 精确撤销当前设备会话。
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { User, Settings, Info, LogOut, Coins, Cpu, FileText } from 'lucide-react';
import { useUserStore } from '../../stores/useUserStore';
import { authApi } from '../../api/auth';

const menuItems = [
  { to: '/profile', icon: User, labelKey: 'nav.profile' },
  { to: '/usage', icon: Coins, labelKey: 'nav.usage' },
  { to: '/memory', icon: FileText, labelKey: 'nav.memory' },
  { to: '/model-config', icon: Cpu, labelKey: 'nav.model_config' },
  { to: '/settings', icon: Settings, labelKey: 'nav.settings' },
  { to: '/about', icon: Info, labelKey: 'nav.about' },
];

export default function UserAvatarMenu() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const userInfo = useUserStore((s) => s.userInfo);
  const logout = useUserStore((s) => s.logout);

  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // 点击菜单外部时关闭
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  /** 处理登出（即使后端失败也清除本地状态） */
  const handleLogout = async () => {
    setOpen(false);
    try {
      const deviceId = localStorage.getItem('device_id') || undefined;
      await authApi.logout(deviceId);
    } catch {
      // 即使后端失败也清除本地状态
    }
    logout();
    navigate('/login');
  };

  return (
    <div className="relative" ref={menuRef}>
      {/* 头像按钮 */}
      <button
        onClick={() => setOpen(!open)}
        className="w-9 h-9 rounded-full overflow-hidden border border-[var(--color-border)]
          hover:border-[var(--color-accent)] transition-colors duration-200
          flex items-center justify-center"
        title={t('topbar.userMenu')}
      >
        {userInfo?.avatar ? (
          <img src={userInfo.avatar} alt="avatar" className="w-full h-full object-cover" />
        ) : (
          <span className="w-full h-full bg-[var(--color-accent-bg)] text-[var(--color-accent)] text-sm font-medium flex items-center justify-center">
            {userInfo?.username?.charAt(0)?.toUpperCase() ?? 'U'}
          </span>
        )}
      </button>

      {/* 下拉菜单 */}
      {open && (
        <div
          className="absolute right-0 top-11 w-44 py-1.5 rounded-[var(--radius-md)]
            bg-[var(--color-card)] border border-[var(--color-border)]
            shadow-[var(--shadow-dialog)] animate-fade-in"
          style={{ zIndex: 101 }}
        >
          {/* 用户信息区 */}
          <div className="px-3 py-2 border-b border-[var(--color-border)]">
            <p className="text-sm font-medium text-[var(--color-text)] truncate">
              {userInfo?.username ?? '-'}
            </p>
            {userInfo?.email && (
              <p className="text-xs text-[var(--color-text-tertiary)] truncate">{userInfo.email}</p>
            )}
          </div>

          {/* 菜单项：个人信息 / 设置 / 关于 */}
          {menuItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.to}
                onClick={() => {
                  setOpen(false);
                  navigate(item.to);
                }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-[var(--color-text-secondary)]
                  hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-accent)] transition-colors duration-150"
              >
                <Icon size={15} />
                {t(item.labelKey)}
              </button>
            );
          })}

          {/* 登出 */}
          <div className="my-1 border-t border-[var(--color-border)]" />
          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-[var(--color-danger)]
              hover:bg-[var(--color-danger-bg)] transition-colors duration-150"
          >
            <LogOut size={15} />
            {t('nav.logout')}
          </button>
        </div>
      )}
    </div>
  );
}
