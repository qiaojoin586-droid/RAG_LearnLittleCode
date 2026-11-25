/**
 * 图形验证码（纯前端生成 + 本地校验，不经过后端）
 *
 * 相对旧实现的改动：
 * 1. 颜色改从 CSS 令牌读取，暗色下不再是刺眼的白块；
 * 2. 「生成码」与「绘制」拆开——主题切换时只重绘、不换答案（旧实现两者耦合，重绘会偷偷换码）；
 * 3. HiDPI 屏按 devicePixelRatio 放大画布，消除模糊；
 * 4. 外观与新表单统一（药丸形、与输入框同高）。
 *
 * 父组件通过 ref 拿到 refresh()，在登录失败时主动换一张。
 */

import { useCallback, useEffect, useImperativeHandle, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { Ref } from 'react';

/** 画布 CSS 尺寸 */
const W = 110;
const H = 48;

/** 去掉了易混淆的 0/O、1/I/l */
const CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789';

export interface AuthCaptchaHandle {
  /** 换一张新验证码（新码会经 onGenerated 回调给父组件） */
  refresh: () => void;
}

export function createCaptchaCode(len = 4): string {
  let code = '';
  for (let i = 0; i < len; i++) {
    code += CHARS[Math.floor(Math.random() * CHARS.length)];
  }
  return code;
}

interface CaptchaTokens {
  bgFrom: string;
  bgTo: string;
  noise: string;
  charA: string;
  charB: string;
}

/** 从 CSS 令牌取色；变量未定义时回退到合理默认值 */
export function readCaptchaTokens(cs: { getPropertyValue(k: string): string }): CaptchaTokens {
  const v = (key: string, fallback: string) => cs.getPropertyValue(key).trim() || fallback;
  return {
    bgFrom: v('--color-accent-bg', '#e8f0fe'),
    bgTo: v('--color-card', '#ffffff'),
    noise: v('--color-accent', '#1677ff'),
    charA: v('--color-accent', '#1677ff'),
    charB: v('--color-text', '#1a2a4a'),
  };
}

/**
 * 把给定的码画到画布上（不生成码）。
 * jsdom 的 getContext('2d') 返回 null —— 必须安全退出，否则测试环境直接抛错。
 */
export function drawCaptcha(canvas: HTMLCanvasElement, code: string): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  // HiDPI：按 dpr 放大位图，再缩放绘图坐标，之后一律按 CSS 像素作图
  const dpr = Math.min(typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1, 2);
  if (canvas.width !== W * dpr || canvas.height !== H * dpr) {
    canvas.width = W * dpr;
    canvas.height = H * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const tokens = readCaptchaTokens(getComputedStyle(canvas));

  // 底色
  const grad = ctx.createLinearGradient(0, 0, W, H);
  grad.addColorStop(0, tokens.bgFrom);
  grad.addColorStop(1, tokens.bgTo);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);

  // 干扰曲线（透明度走 globalAlpha，避免手写 rgba 拼接）
  ctx.strokeStyle = tokens.noise;
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    ctx.globalAlpha = 0.15 + Math.random() * 0.15;
    ctx.beginPath();
    ctx.moveTo(Math.random() * W, Math.random() * H);
    ctx.bezierCurveTo(
      Math.random() * W, Math.random() * H,
      Math.random() * W, Math.random() * H,
      Math.random() * W, Math.random() * H,
    );
    ctx.stroke();
  }

  // 干扰点
  ctx.fillStyle = tokens.noise;
  for (let i = 0; i < 30; i++) {
    ctx.globalAlpha = 0.2 + Math.random() * 0.3;
    ctx.beginPath();
    ctx.arc(Math.random() * W, Math.random() * H, 1, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  // 字符：两色交替（暗色下随机浅蓝会忽明忽暗，故不做色相抖动）
  ctx.font = 'bold 22px "JetBrains Mono", "Courier New", monospace';
  ctx.textBaseline = 'middle';
  for (let i = 0; i < code.length; i++) {
    ctx.save();
    ctx.translate(16 + i * 24, H / 2);
    ctx.rotate((Math.random() - 0.5) * 0.5);
    ctx.fillStyle = i % 2 === 0 ? tokens.charA : tokens.charB;
    ctx.fillText(code[i], 0, 0);
    ctx.restore();
  }
}

interface Props {
  /** React 19：ref 作为普通 prop 传入 */
  ref?: Ref<AuthCaptchaHandle>;
  /** 每次生成新码时回调，父组件保存用于本地校验 */
  onGenerated: (code: string) => void;
}

export default function AuthCaptcha({ ref, onGenerated }: Props) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const codeRef = useRef('');
  const cbRef = useRef(onGenerated);

  // 回调镜像：避免父组件未 memo 时反复重建 refresh / 重跑 effect
  useEffect(() => {
    cbRef.current = onGenerated;
  }, [onGenerated]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (canvas) drawCaptcha(canvas, codeRef.current);
  }, []);

  const refresh = useCallback(() => {
    codeRef.current = createCaptchaCode(4);
    cbRef.current(codeRef.current);
    draw();
  }, [draw]);

  useImperativeHandle(ref, () => ({ refresh }), [refresh]);

  // 首次生成 + 主题切换重绘
  useEffect(() => {
    refresh();
    if (typeof MutationObserver === 'undefined') return;
    // 子组件的 effect 先于 App.tsx「往 <html> 加 dark class」的 effect 运行，
    // 因此监听主题 state 会读到旧令牌值；直接观察 class 变化才拿得到新值
    const mo = new MutationObserver(draw);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => mo.disconnect();
  }, [refresh, draw]);

  return (
    <canvas
      ref={canvasRef}
      onClick={refresh}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          refresh();
        }
      }}
      role="button"
      tabIndex={0}
      aria-label={t('auth.captchaLabel')}
      title={t('auth.captchaRefresh')}
      className="h-12 shrink-0 cursor-pointer rounded-full border border-[var(--color-border)]
        transition-opacity hover:opacity-80
        focus-visible:shadow-[0_0_0_3px_var(--color-accent-bg)]"
      style={{ width: W, height: H }}
    />
  );
}
