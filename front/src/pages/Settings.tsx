/**
 * 设置页面
 * 
 * 功能：
 * 1. 主题切换（亮色 / 暗色）
 * 2. 语言切换（中文 / English）
 * 3. 修改密码
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Sun, Moon, Globe, Lock, Save } from 'lucide-react';
import { useThemeStore } from '../stores/useThemeStore';
import { useLanguageStore } from '../stores/useLanguageStore';
import { userApi } from '../api/user';

export default function Settings() {
  const { t, i18n } = useTranslation();

  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);

  const language = useLanguageStore((s) => s.lang);
  const setLanguage = useLanguageStore((s) => s.setLang);

  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [changingPassword, setChangingPassword] = useState(false);

  /** 切换语言并同步 i18next */
  const handleLanguageChange = (lang: 'zh-CN' | 'en-US') => {
    setLanguage(lang);
    i18n.changeLanguage(lang);
  };

  /** 提交密码修改 */
  const handleChangePassword = async () => {
    // 前端校验
    if (!oldPassword || !newPassword) {
      toast.error('请填写旧密码和新密码');
      return;
    }
    if (newPassword.length < 8) {
      toast.error('新密码至少 8 位');
      return;
    }
    if (!/[a-zA-Z]/.test(newPassword) || !/\d/.test(newPassword)) {
      toast.error('密码需同时包含字母和数字');
      return;
    }
    if (newPassword !== confirmPassword) {
      toast.error('两次输入的密码不一致');
      return;
    }

    setChangingPassword(true);
    try {
      await userApi.changePassword({ old_password: oldPassword, new_password: newPassword });
      toast.success('密码修改成功');
      setOldPassword('');
      setNewPassword('');
      setConfirmPassword('');
    } catch {
      toast.error('密码修改失败，请检查旧密码是否正确');
    } finally {
      setChangingPassword(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-heading font-bold text-[var(--color-text)] mb-6">
        {t('nav.settings')}
      </h1>

      <div className="space-y-6">
        {/* ===== 主题设置 ===== */}
        <section className="p-5 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)]">
          <h2 className="flex items-center gap-2 text-base font-medium text-[var(--color-text)] mb-4">
            {theme === 'light' ? <Sun size={18} /> : <Moon size={18} />}
            {t('settings.theme')}
          </h2>
          <div className="flex gap-3">
            {(['light', 'dark'] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => setTheme(mode)}
                className={`
                  flex items-center gap-2 px-4 py-2 rounded-[var(--radius-md)] border text-sm transition-all
                  ${theme === mode
                    ? 'border-[var(--color-accent)] bg-[var(--color-accent-bg)] text-[var(--color-accent)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg)]'
                  }
                `}
              >
                {mode === 'light' ? <Sun size={16} /> : <Moon size={16} />}
                {mode === 'light' ? '亮色' : '暗色'}
              </button>
            ))}
          </div>
        </section>

        {/* ===== 语言设置 ===== */}
        <section className="p-5 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)]">
          <h2 className="flex items-center gap-2 text-base font-medium text-[var(--color-text)] mb-4">
            <Globe size={18} />
            {t('settings.language')}
          </h2>
          <div className="flex gap-3">
            {[
              { value: 'zh-CN' as const, label: '中文' },
              { value: 'en-US' as const, label: 'English' },
            ].map((lang) => (
              <button
                key={lang.value}
                onClick={() => handleLanguageChange(lang.value)}
                className={`
                  px-4 py-2 rounded-[var(--radius-md)] border text-sm transition-all
                  ${language === lang.value
                    ? 'border-[var(--color-accent)] bg-[var(--color-accent-bg)] text-[var(--color-accent)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg)]'
                  }
                `}
              >
                {lang.label}
              </button>
            ))}
          </div>
        </section>

        {/* ===== 修改密码 ===== */}
        <section className="p-5 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)]">
          <h2 className="flex items-center gap-2 text-base font-medium text-[var(--color-text)] mb-4">
            <Lock size={18} />
            {t('settings.changePassword')}
          </h2>
          <div className="space-y-3">
            <div>
              <label className="block text-sm text-[var(--color-text-secondary)] mb-1">
                {t('settings.oldPassword')}
              </label>
              <input
                type="password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                className="w-full px-3 py-2 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
            </div>
            <div>
              <label className="block text-sm text-[var(--color-text-secondary)] mb-1">
                {t('settings.newPassword')}
              </label>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className="w-full px-3 py-2 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">密码需8位以上，且必须同时包含字母和数字</p>
            </div>
            <div>
              <label className="block text-sm text-[var(--color-text-secondary)] mb-1">
                {t('auth.confirmPassword')}
              </label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full px-3 py-2 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">密码需8位以上，且必须同时包含字母和数字</p>
            </div>
            <button
              onClick={handleChangePassword}
              disabled={changingPassword}
              className="flex items-center gap-2 px-5 py-2 rounded-[var(--radius-md)] bg-[var(--color-accent)] text-white text-sm hover:opacity-90 disabled:opacity-50"
            >
              <Save size={16} />
              {changingPassword ? '提交中...' : t('settings.changePassword')}
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}
