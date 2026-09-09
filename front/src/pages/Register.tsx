/**
 * 注册页面
 *
 * 视觉与交互规格：docs/auth-pages-vite-spec.html（与登录页共用 AuthShell）
 *
 * 功能（与旧版一致，未做增减）：
 * 1. 用户名 + 邮箱 + 6 位邮箱验证码 + 密码 + 确认密码
 * 2. 前端校验密码一致性等规则
 * 3. 注册成功后跳转登录页
 *
 * 顺带修复：旧版在 async 处理函数里 setInterval 且无清理，组件卸载后仍会
 * 对已卸载组件 setState；现把定时器 id 存进 ref 并在 unmount 时清除。
 */

import { useState, useEffect, useRef } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { User, Mail, Lock, ShieldCheck } from 'lucide-react';
import { authApi } from '../api/auth';
import AuthShell from '../components/auth/AuthShell';
import { AuthInput, PasswordField } from '../components/auth/AuthInput';
import InteractiveHoverButton from '../components/auth/InteractiveHoverButton';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const CODE_LENGTH = 6;

export default function Register() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [verificationCode, setVerificationCode] = useState('');
  const [countdown, setCountdown] = useState(0);
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);

  const countdownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 卸载时清理倒计时，避免对已卸载组件 setState
  useEffect(() => () => {
    if (countdownTimerRef.current !== null) {
      clearInterval(countdownTimerRef.current);
      countdownTimerRef.current = null;
    }
  }, []);

  /** 发送邮箱验证码（60s 倒计时防重复点击） */
  const handleSendCode = async () => {
    if (!EMAIL_RE.test(email)) {
      toast.error(t('auth.invalidEmail'));
      return;
    }
    if (countdown > 0) return;

    try {
      await authApi.sendVerificationCode(email);
      toast.success(t('auth.codeSent'));
      setCountdown(60);
      countdownTimerRef.current = setInterval(() => {
        setCountdown((prev) => {
          if (prev <= 1) {
            if (countdownTimerRef.current !== null) {
              clearInterval(countdownTimerRef.current);
              countdownTimerRef.current = null;
            }
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || t('auth.codeSendFailed'));
    }
  };

  /** 提交注册 */
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();

    // 前端校验
    if (!EMAIL_RE.test(email)) {
      toast.error(t('auth.invalidEmail'));
      return;
    }
    if (verificationCode.length !== CODE_LENGTH) {
      toast.error(t('auth.invalidCode'));
      return;
    }
    if (password.length < 8) {
      toast.error(t('auth.passwordTooShort'));
      return;
    }
    if (!/[a-zA-Z]/.test(password) || !/\d/.test(password)) {
      toast.error(t('auth.passwordWeak'));
      return;
    }
    if (password !== confirmPassword) {
      toast.error(t('auth.passwordMismatch'));
      return;
    }

    setLoading(true);
    try {
      await authApi.register({
        username,
        password,
        email,
        verification_code: verificationCode,
      });

      toast.success(t('auth.registerSuccess'));
      navigate('/login');
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || t('auth.registerFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthShell showPassword={showPassword} passwordLength={password.length}>
      {/* 标题区 */}
      <div className="mb-10 text-center">
        <h1 className="text-2xl font-semibold text-[var(--color-text)]">{t('auth.createAccount')}</h1>
        <p className="mt-1.5 text-sm text-[var(--color-text-secondary)]">{t('auth.registerSubtitle')}</p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        <AuthInput
          icon={User}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder={t('auth.usernamePlaceholder')}
          autoComplete="username"
          minLength={3}
          maxLength={50}
          required
        />

        {/* 邮箱 + 发送验证码（与验证码行同款并排布局） */}
        <div className="flex items-start gap-3">
          <AuthInput
            icon={Mail}
            wrapClassName="flex-1"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t('auth.emailPlaceholder')}
            autoComplete="email"
            required
          />
          <button
            type="button"
            onClick={handleSendCode}
            disabled={countdown > 0}
            className="h-12 shrink-0 rounded-full bg-[var(--color-accent-bg)] px-4 text-sm font-medium
              text-[var(--color-accent)] transition-opacity hover:opacity-80 disabled:opacity-50"
          >
            {countdown > 0 ? t('auth.resendCountdown', { s: countdown }) : t('auth.sendCode')}
          </button>
        </div>

        <AuthInput
          icon={ShieldCheck}
          value={verificationCode}
          onChange={(e) => setVerificationCode(e.target.value.replace(/\D/g, '').slice(0, CODE_LENGTH))}
          placeholder={t('auth.codePlaceholder')}
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={CODE_LENGTH}
          required
        />

        <div>
          <PasswordField
            icon={Lock}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            visible={showPassword}
            onToggleVisible={() => setShowPassword((v) => !v)}
            placeholder={t('auth.passwordPlaceholder')}
            autoComplete="new-password"
            minLength={8}
            required
          />
          <p className="mt-1.5 pl-4 text-xs text-[var(--color-text-tertiary)]">
            {t('auth.passwordHint')}
          </p>
        </div>

        <PasswordField
          icon={Lock}
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          visible={showPassword}
          onToggleVisible={() => setShowPassword((v) => !v)}
          placeholder={t('auth.confirmPasswordPlaceholder')}
          autoComplete="new-password"
          required
        />

        <InteractiveHoverButton
          type="submit"
          label={t('auth.register')}
          loadingLabel={t('auth.registering')}
          loading={loading}
        />
      </form>

      {/* 底部切换入口 */}
      <p className="mt-8 text-center text-sm text-[var(--color-text-secondary)]">
        {t('auth.hasAccount')}{' '}
        <Link
          to="/login"
          className="font-medium text-[var(--color-accent)] transition-opacity hover:opacity-80"
        >
          {t('auth.signInNow')}
        </Link>
      </p>
    </AuthShell>
  );
}
