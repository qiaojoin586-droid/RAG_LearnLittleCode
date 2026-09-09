/**
 * 登录页面
 *
 * 视觉与交互规格：docs/auth-pages-vite-spec.html
 * 左栏动画角色 + 右栏药丸形表单；配色走项目令牌，随主题自动适配。
 *
 * 功能（与旧版一致，未做增减）：
 * 1. 账号 + 密码 + 图形验证码（验证码为纯前端生成与本地校验，不经过后端）
 * 2. 登录成功后保存 Token 并跳转首页
 * 3. 密码显隐切换、记住账号（仅保存用户名，明文密码严禁落盘）
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { User, Lock, ShieldCheck } from 'lucide-react';
import { authApi } from '../api/auth';
import { userApi } from '../api/user';
import { useUserStore } from '../stores/useUserStore';
import AuthShell from '../components/auth/AuthShell';
import AuthCaptcha from '../components/auth/AuthCaptcha';
import type { AuthCaptchaHandle } from '../components/auth/AuthCaptcha';
import { AuthInput, PasswordField } from '../components/auth/AuthInput';
import InteractiveHoverButton from '../components/auth/InteractiveHoverButton';

export default function Login() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const login = useUserStore((s) => s.login);

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [captchaInput, setCaptchaInput] = useState('');
  const [rememberPwd, setRememberPwd] = useState(true);
  const [loading, setLoading] = useState(false);

  const captchaRef = useRef<AuthCaptchaHandle>(null);
  const captchaCodeRef = useRef('');

  const handleCaptchaGenerated = useCallback((code: string) => {
    captchaCodeRef.current = code;
  }, []);

  // 记住账号：页面加载时恢复保存的用户名（安全审查 P0-6：明文密码严禁落盘，
  // 故此处只恢复用户名，不恢复密码；自动登录需后端签发一次性 remember-me 票据后才可支持）
  useEffect(() => {
    const saved = localStorage.getItem('remembered_login');
    if (saved) {
      try {
        const { username: u } = JSON.parse(saved);
        setUsername(u || '');
        setRememberPwd(true);
      } catch { /* ignore */ }
    }
  }, []);

  useEffect(() => {
    if (!localStorage.getItem('device_id')) {
      const id = crypto.randomUUID?.() || `dev_${Date.now()}_${Math.random().toString(36).slice(2)}`;
      localStorage.setItem('device_id', id);
    }
  }, []);

  const getDeviceName = (): string => {
    const ua = navigator.userAgent;
    let browser = 'Unknown';
    if (ua.includes('Firefox')) browser = 'Firefox';
    else if (ua.includes('Edg')) browser = 'Edge';
    else if (ua.includes('Chrome')) browser = 'Chrome';
    else if (ua.includes('Safari')) browser = 'Safari';
    let os = 'Unknown';
    if (ua.includes('Windows')) os = 'Windows';
    else if (ua.includes('Mac')) os = 'macOS';
    else if (ua.includes('Linux')) os = 'Linux';
    else if (ua.includes('Android')) os = 'Android';
    else if (ua.includes('iOS') || ua.includes('iPhone') || ua.includes('iPad')) os = 'iOS';
    return `${browser} on ${os}`;
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username || !password) return;

    // 验证码校验（不区分大小写）
    if (!captchaInput || captchaInput.toLowerCase() !== captchaCodeRef.current.toLowerCase()) {
      toast.error(t('auth.captchaError'));
      captchaRef.current?.refresh();
      setCaptchaInput('');
      return;
    }

    // 记住账号：仅保存用户名（安全审查 P0-6，明文密码不落盘）
    if (rememberPwd) {
      localStorage.setItem('remembered_login', JSON.stringify({ username }));
    } else {
      localStorage.removeItem('remembered_login');
    }

    setLoading(true);
    try {
      const deviceId = localStorage.getItem('device_id') || undefined;
      const deviceName = getDeviceName();

      const res = await authApi.login({ username, password, device_id: deviceId, device_name: deviceName });
      const { access_token, refresh_token } = res.data.data;

      localStorage.setItem('jwt_token', access_token);
      localStorage.setItem('jwt_refresh_token', refresh_token);

      const userRes = await userApi.getMe();
      const userInfo = userRes.data.data;

      login(access_token, refresh_token, userInfo, deviceId);

      toast.success(t('auth.loginSuccess'));
      navigate('/');
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || t('auth.loginFailed'));
      captchaRef.current?.refresh();
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthShell showPassword={showPassword} passwordLength={password.length}>
      {/* 标题区 */}
      <div className="mb-10 text-center">
        <h1 className="text-2xl font-semibold text-[var(--color-text)]">{t('auth.accountLogin')}</h1>
        <p className="mt-1.5 text-sm text-[var(--color-text-secondary)]">{t('auth.loginSubtitle')}</p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        <AuthInput
          icon={User}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder={t('auth.usernamePlaceholder')}
          autoComplete="username"
          required
        />

        <PasswordField
          icon={Lock}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          visible={showPassword}
          onToggleVisible={() => setShowPassword((v) => !v)}
          placeholder={t('auth.passwordPlaceholder')}
          autoComplete="current-password"
          required
        />

        {/* 验证码：输入框 + 画布同高并排 */}
        <div className="flex items-start gap-3">
          <AuthInput
            icon={ShieldCheck}
            wrapClassName="flex-1"
            value={captchaInput}
            onChange={(e) => setCaptchaInput(e.target.value)}
            placeholder={t('auth.captchaPlaceholder')}
            maxLength={4}
            autoComplete="off"
            required
          />
          <AuthCaptcha ref={captchaRef} onGenerated={handleCaptchaGenerated} />
        </div>

        {/* 选项栏 */}
        <div className="flex items-center justify-between">
          <label className="flex cursor-pointer select-none items-center gap-2 text-sm text-[var(--color-text-secondary)]">
            <input
              type="checkbox"
              checked={rememberPwd}
              onChange={(e) => setRememberPwd(e.target.checked)}
              className="h-4 w-4 cursor-pointer accent-[var(--color-accent)]"
            />
            {t('auth.rememberMe')}
          </label>
          <button
            type="button"
            className="text-sm text-[var(--color-accent)] transition-opacity hover:opacity-80"
          >
            {t('auth.forgotPassword')}
          </button>
        </div>

        <InteractiveHoverButton
          type="submit"
          label={t('auth.login')}
          loadingLabel={t('auth.loggingIn')}
          loading={loading}
        />
      </form>

      {/* 底部切换入口 */}
      <p className="mt-8 text-center text-sm text-[var(--color-text-secondary)]">
        {t('auth.noAccount')}{' '}
        <Link
          to="/register"
          className="font-medium text-[var(--color-accent)] transition-opacity hover:opacity-80"
        >
          {t('auth.registerNow')}
        </Link>
      </p>
    </AuthShell>
  );
}
