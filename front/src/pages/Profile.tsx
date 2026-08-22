/**
 * 个人信息页面
 * 
 * 功能：
 * 1. 展示用户基本信息（用户名、邮箱、简介）
 * 2. 头像上传 + 实时预览
 * 3. 编辑个人资料（邮箱、简介）
 */

import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Camera, Mail, Save, User } from 'lucide-react';
import { userApi } from '../api/user';
import { authApi } from '../api/auth';
import { useUserStore } from '../stores/useUserStore';
import LoadingSkeleton from '../components/common/LoadingSkeleton';
import type { UserInfo } from '../types/api';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function Profile() {
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const userInfo = useUserStore((s) => s.userInfo);
  const updateUserInfo = useUserStore((s) => s.updateUserInfo);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [email, setEmail] = useState('');
  const [emailVerified, setEmailVerified] = useState(false);
  const [editingEmail, setEditingEmail] = useState(false);
  const [newEmail, setNewEmail] = useState('');
  const [emailCode, setEmailCode] = useState('');
  const [emailCountdown, setEmailCountdown] = useState(0);
  const [changingEmail, setChangingEmail] = useState(false);
  const [bio, setBio] = useState('');
  const [avatarUrl, setAvatarUrl] = useState<string | null>(null);
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [uploadingAvatar, setUploadingAvatar] = useState(false);

  /** 加载用户信息 */
  useEffect(() => {
    (async () => {
      try {
        const res = await userApi.getMe();
        const user: UserInfo = res.data.data;
        updateUserInfo(user);
        setEmail(user.email || '');
        setEmailVerified(user.email_verified);
        setBio(user.bio || '');
        setAvatarUrl(user.avatar);
      } catch {
        toast.error('加载用户信息失败');
      } finally {
        setLoading(false);
      }
    })();
    // updateUserInfo 为 zustand 稳定 action，加入依赖不会重复触发
  }, [updateUserInfo]);

  /** 头像文件选择 → 预览 + 上传 */
  const handleAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // 前端预览
    const previewUrl = URL.createObjectURL(file);
    setAvatarPreview(previewUrl);

    // 调用后端上传接口
    setUploadingAvatar(true);
    try {
      const res = await userApi.uploadAvatar(file);
      const { avatar_url } = res.data.data;
      setAvatarUrl(avatar_url);
      setAvatarPreview(null);
      updateUserInfo({ avatar: avatar_url });
      toast.success('头像更新成功');
    } catch {
      toast.error('头像上传失败');
      setAvatarPreview(null);
    } finally {
      setUploadingAvatar(false);
      e.target.value = '';
    }
  };

  /** 保存资料修改（邮箱已独立为验证模块，此处只保存简介） */
  const handleSave = async () => {
    setSaving(true);
    try {
      await userApi.updateMe({ bio });
      updateUserInfo({ bio });
      toast.success('保存成功');
    } catch {
      toast.error(t('common.error'));
    } finally {
      setSaving(false);
    }
  };

  /** 修改邮箱流程 ①：发送验证码到新邮箱 */
  const handleSendEmailCode = async () => {
    if (!EMAIL_RE.test(newEmail)) {
      toast.error('请输入正确的邮箱地址');
      return;
    }
    if (emailCountdown > 0) return;

    try {
      await authApi.sendVerificationCode(newEmail);
      toast.success('验证码已发送，请查收邮件');
      setEmailCountdown(60);
      const timer = setInterval(() => {
        setEmailCountdown((prev) => {
          if (prev <= 1) {
            clearInterval(timer);
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '验证码发送失败');
    }
  };

  /** 修改邮箱流程 ②：提交新邮箱 + 验证码 */
  const handleConfirmEmailChange = async () => {
    if (!EMAIL_RE.test(newEmail)) {
      toast.error('请输入正确的邮箱地址');
      return;
    }
    if (emailCode.length !== 6) {
      toast.error('请输入 6 位验证码');
      return;
    }

    setChangingEmail(true);
    try {
      await userApi.changeEmail({ email: newEmail, verification_code: emailCode });
      setEmail(newEmail);
      setEmailVerified(true);
      updateUserInfo({ email: newEmail, email_verified: true });
      setEmailCode('');
      setNewEmail('');
      setEditingEmail(false);
      toast.success('邮箱修改成功');
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '邮箱修改失败');
    } finally {
      setChangingEmail(false);
    }
  };

  if (loading) return <LoadingSkeleton />;

  /** 显示的头像地址（预览 > 已保存 > 默认） */
  const displayAvatar = avatarPreview || avatarUrl;

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-heading font-bold text-[var(--color-text)] mb-6">
        {t('nav.profile')}
      </h1>

      {/* 头像区域 */}
      <div className="flex items-center gap-6 mb-8 p-6 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)]">
        {/* 头像展示 */}
        <div className="relative group">
          <div className="w-20 h-20 rounded-full overflow-hidden bg-[var(--color-bg)] border-2 border-[var(--color-border)]">
            {displayAvatar ? (
              <img
                src={displayAvatar}
                alt="头像"
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center">
                <User size={32} className="text-[var(--color-text-tertiary)]" />
              </div>
            )}
          </div>
          {/* 悬浮上传按钮 */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploadingAvatar}
            className="absolute inset-0 flex items-center justify-center rounded-full bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer"
          >
            <Camera size={20} className="text-white" />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp"
            onChange={handleAvatarChange}
            className="hidden"
          />
          {uploadingAvatar && (
            <div className="absolute -bottom-1 left-1/2 -translate-x-1/2 text-xs text-[var(--color-accent)]">
              上传中...
            </div>
          )}
        </div>

        {/* 用户名（只读） */}
        <div>
          <h2 className="text-lg font-bold text-[var(--color-text)]">{userInfo?.username}</h2>
          <p className="text-xs text-[var(--color-text-tertiary)] mt-1">
            注册于 {userInfo?.created_at ? new Date(userInfo.created_at).toLocaleDateString() : '-'}
          </p>
        </div>
      </div>

      {/* 资料编辑表单 */}
      <div className="space-y-4">
        {/* 邮箱（独立验证模块：展示状态 + 修改邮箱需验证码） */}
        <div>
          <label className="block text-sm text-[var(--color-text-secondary)] mb-1">
            {t('auth.email')}
          </label>
          <div className="flex items-center gap-2 flex-wrap">
            <Mail size={15} className="text-[var(--color-text-tertiary)]" />
            <span className="text-sm text-[var(--color-text)]">{email || '未绑定邮箱'}</span>
            {emailVerified ? (
              <span className="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400">
                ✓ 已验证
              </span>
            ) : (
              <span className="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-400">
                未验证
              </span>
            )}
            <button
              onClick={() => setEditingEmail(!editingEmail)}
              className="text-xs text-[var(--color-accent)] hover:underline"
            >
              {editingEmail ? '取消' : '修改邮箱'}
            </button>
          </div>

          {/* 修改邮箱验证流程（两步：发送验证码 → 提交新邮箱+验证码） */}
          {editingEmail && (
            <div className="mt-3 space-y-2 p-3 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)]">
              <div className="flex gap-2">
                <input
                  type="email"
                  value={newEmail}
                  onChange={(e) => setNewEmail(e.target.value)}
                  placeholder="输入新邮箱"
                  className="flex-1 px-3 py-2 text-sm rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
                />
                <button
                  onClick={handleSendEmailCode}
                  disabled={emailCountdown > 0}
                  className="shrink-0 px-3 py-2 text-xs text-white bg-[var(--color-accent)] rounded-[var(--radius-md)] hover:opacity-90 disabled:opacity-50"
                >
                  {emailCountdown > 0 ? `${emailCountdown}s` : '发送验证码'}
                </button>
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={emailCode}
                  onChange={(e) => setEmailCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  placeholder="输入 6 位验证码"
                  inputMode="numeric"
                  maxLength={6}
                  className="flex-1 px-3 py-2 text-sm rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
                />
                <button
                  onClick={handleConfirmEmailChange}
                  disabled={changingEmail}
                  className="shrink-0 px-3 py-2 text-xs text-white bg-[var(--color-accent)] rounded-[var(--radius-md)] hover:opacity-90 disabled:opacity-50"
                >
                  {changingEmail ? '提交中...' : '确认修改'}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* 个人简介 */}
        <div>
          <label className="block text-sm text-[var(--color-text-secondary)] mb-1">
            个人简介
          </label>
          <textarea
            value={bio}
            onChange={(e) => setBio(e.target.value)}
            placeholder="写点什么介绍自己..."
            rows={3}
            className="w-full px-3 py-2 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)] resize-none focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
          />
        </div>

        {/* 保存按钮 */}
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 px-5 py-2 rounded-[var(--radius-md)] bg-[var(--color-accent)] text-white text-sm hover:opacity-90 disabled:opacity-50"
        >
          <Save size={16} />
          {saving ? '保存中...' : t('common.save')}
        </button>
      </div>
    </div>
  );
}
