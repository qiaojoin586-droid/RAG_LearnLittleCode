/**
 * 认证页表单控件
 *
 * - AuthInput：药丸形输入框（h-12 + rounded-full），左图标 + 右插槽
 * - PasswordField：AuthInput + 眼睛显隐按钮
 *
 * 这两页共 9 个输入框，此前每个都复制粘贴了同一串 8 行 className（且写死 #1677ff），
 * 抽到这里统一。
 *
 * ⚠️ 样式一律走 --color-* 令牌，且**不得**使用 `xxx-[var(--color-x)]/20` 这类透明度
 * 修饰符——Tailwind v3 对 var() 任意值不支持 opacity modifier，会把整条规则静默丢弃
 * （项目里已有 6 处中招）。需要浅色底时用成对令牌 --color-accent-bg / --color-danger-bg。
 */

import { useTranslation } from 'react-i18next';
import { Eye, EyeOff } from 'lucide-react';
import type { ComponentType, InputHTMLAttributes, ReactNode } from 'react';

/** lucide-react v1.25 未导出 LucideIcon 类型，这里按实际用法声明 */
type IconComponent = ComponentType<{ size?: number | string; className?: string }>;

export interface AuthInputProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, 'className' | 'size'> {
  /** 左侧图标 */
  icon?: IconComponent;
  /** 右侧内嵌插槽（眼睛按钮 / 发送验证码 / 验证码图片） */
  trailing?: ReactNode;
  /** 字段级错误文案（红色边框 + 下方提示） */
  error?: string;
  /** 仅用于外层布局（如 flex-1），不覆盖内部类名 */
  wrapClassName?: string;
  /** 追加到 <input> 自身的类名（内部类名仍保留），用于挂伪元素规则等 */
  inputClassName?: string;
}

export function AuthInput({
  icon: Icon,
  trailing,
  error,
  wrapClassName = '',
  inputClassName = '',
  ...inputProps
}: AuthInputProps) {
  return (
    <div className={`relative ${wrapClassName}`}>
      {Icon && (
        <Icon
          size={16}
          className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]"
        />
      )}
      <input
        {...inputProps}
        className={`h-12 w-full rounded-full border bg-[var(--color-card)] text-sm
          text-[var(--color-text)] placeholder:text-[var(--color-text-tertiary)]
          outline-none transition-colors duration-150
          hover:border-[var(--color-text-tertiary)]
          focus:border-[var(--color-accent)] focus:shadow-[0_0_0_3px_var(--color-accent-bg)]
          disabled:opacity-50
          ${error ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]'}
          ${Icon ? 'pl-11' : 'pl-4'}
          ${trailing ? 'pr-12' : 'pr-4'}
          ${inputClassName}`}
        aria-invalid={error ? true : undefined}
      />
      {trailing && (
        <div className="absolute right-2 top-1/2 -translate-y-1/2">{trailing}</div>
      )}
      {error && (
        <p role="alert" className="mt-1.5 pl-4 text-[13px] text-[var(--color-danger)]">
          {error}
        </p>
      )}
    </div>
  );
}

export interface PasswordFieldProps
  extends Omit<AuthInputProps, 'type' | 'trailing' | 'inputClassName'> {
  /** 当前是否明文可见 */
  visible: boolean;
  /** 切换明暗 */
  onToggleVisible: () => void;
}

export function PasswordField({ visible, onToggleVisible, ...rest }: PasswordFieldProps) {
  const { t } = useTranslation();
  return (
    <AuthInput
      {...rest}
      // 置于 rest 之后：确保不被调用方覆盖，伪元素规则一定生效
      inputClassName="auth-password"
      type={visible ? 'text' : 'password'}
      trailing={
        <button
          type="button"
          onClick={onToggleVisible}
          aria-label={visible ? t('auth.hidePassword') : t('auth.showPassword')}
          aria-pressed={visible}
          className="flex h-9 w-9 items-center justify-center rounded-full
            text-[var(--color-text-tertiary)] transition-colors
            hover:text-[var(--color-text)]"
        >
          {visible ? <EyeOff size={18} /> : <Eye size={18} />}
        </button>
      }
    />
  );
}
