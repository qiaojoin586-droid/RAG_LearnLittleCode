/**
 * 动画角色舞台（登录 / 注册页左栏）
 *
 * 规格来源：docs/auth-pages-vite-spec.html
 * - 画布 550×400，四个角色全部 position:absolute; bottom:0，靠 z-index 分层
 * - 四色硬编码（规格明确要求不进 CSS 变量，换配色改下面 4 个字面量即可）：
 *     紫 #6c3ff5 / 黑 #2d2d2d / 橙 #ff9b6b / 黄 #e8d754
 *
 * 行为（对应规格的「动效时序」表）：
 * - 鼠标移动：瞳孔朝鼠标偏移（≤5px），面部 ±15/±10，身体 skew ±6deg
 * - 随机眨眼：紫 + 黑，眼球高度→2px 持续 150ms，间隔 3000–7000ms
 * - 输入框聚焦：紫黑「对视」800ms（由 isTyping 上升沿触发）
 * - 密码可见且已输入：全员 skewX(0) 集体转头 + 紫色周期性「偷看」800ms，间隔 2000–5000ms
 *
 * 性能：mousemove 不触发 React 重渲染。所有更新走命令式 render()，直接改 DOM style——
 * 若改用 setState，60fps 的鼠标移动会重渲染整棵组件树。props 经 stateRef 镜像，
 * 使 render() 身份稳定、副作用无需随 props 重订阅。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

/** 角色四色（规格要求硬编码） */
const CH_PURPLE = '#6c3ff5';
const CH_BLACK = '#2d2d2d';
const CH_ORANGE = '#ff9b6b';
const CH_YELLOW = '#e8d754';

/** 画布尺寸 */
const CANVAS_W = 550;
const CANVAS_H = 400;

/** 瞳孔朝鼠标偏移的最大距离（视口像素） */
const PUPIL_MAX_DIST = 5;

interface Props {
  /** 任一输入框聚焦中 */
  isTyping: boolean;
  /** 密码是否明文可见 */
  showPassword: boolean;
  /** 密码已输入长度 */
  passwordLength: number;
}

interface CharState {
  isTyping: boolean;
  showPassword: boolean;
  passwordLength: number;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

/** 身体公共过渡（规格：700ms ease-in-out） */
const BODY_TRANSITION =
  'transform 700ms ease-in-out, height 700ms ease-in-out, left 700ms ease-in-out, top 700ms ease-in-out';
/** 后排眼睛过渡 700ms；前排（橙 / 黄）跟进更快，单独覆盖为 200ms ease-out */
const EYES_TRANSITION = 'left 700ms ease-in-out, top 700ms ease-in-out';
const EYES_TRANSITION_FAST = 'left 200ms ease-out, top 200ms ease-out';

export default function AnimatedCharacters({ isTyping, showPassword, passwordLength }: Props) {
  // ── 状态镜像：render() 从这里读最新值，避免闭包过期与无谓的副作用重订阅 ──
  const stateRef = useRef<CharState>({ isTyping, showPassword, passwordLength });
  const mouse = useRef({ x: 0, y: 0 });
  const peeking = useRef(false);
  const lookingAtEachOther = useRef(false);
  /** rAF 句柄：把一帧内的多次 mousemove 合并成一次 DOM 写入 */
  const rafRef = useRef<number | null>(null);

  // ── 缩放：把 550×400 画布适配不同宽度的左栏 ──
  const holderRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);

  // ── 角色与面部节点 ──
  const purpleRef = useRef<HTMLDivElement>(null);
  const blackRef = useRef<HTMLDivElement>(null);
  const orangeRef = useRef<HTMLDivElement>(null);
  const yellowRef = useRef<HTMLDivElement>(null);
  const purpleEyesRef = useRef<HTMLDivElement>(null);
  const blackEyesRef = useRef<HTMLDivElement>(null);
  const orangeEyesRef = useRef<HTMLDivElement>(null);
  const yellowEyesRef = useRef<HTMLDivElement>(null);
  const mouthRef = useRef<HTMLDivElement>(null);

  // 白眼球（仅紫 / 黑，眨眼时改高度）
  const purpleEyeballs = useRef<(HTMLDivElement | null)[]>([]);
  const blackEyeballs = useRef<(HTMLDivElement | null)[]>([]);
  // 瞳孔（四个角色都有）
  const purplePupils = useRef<(HTMLDivElement | null)[]>([]);
  const blackPupils = useRef<(HTMLDivElement | null)[]>([]);
  const orangePupils = useRef<(HTMLDivElement | null)[]>([]);
  const yellowPupils = useRef<(HTMLDivElement | null)[]>([]);

  /** 鼠标相对角色中心的偏移 → 面部位移与身体倾斜 */
  const faceOf = useCallback((el: HTMLElement | null) => {
    if (!el) return { faceX: 0, faceY: 0, bodySkew: 0 };
    const r = el.getBoundingClientRect();
    // 纵向取 1/3 处作为「面部中心」（规格如此，与视觉重心一致）
    const dx = mouse.current.x - (r.left + r.width / 2);
    const dy = mouse.current.y - (r.top + r.height / 3);
    return {
      faceX: clamp(dx / 20, -15, 15),
      faceY: clamp(dy / 30, -10, 10),
      bodySkew: clamp(-dx / 120, -6, 6),
    };
  }, []);

  /** 单只瞳孔朝鼠标方向偏移，最多 PUPIL_MAX_DIST 像素 */
  const pupilOffset = useCallback((el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const dx = mouse.current.x - (r.left + r.width / 2);
    const dy = mouse.current.y - (r.top + r.height / 2);
    const d = Math.min(Math.hypot(dx, dy), PUPIL_MAX_DIST);
    const a = Math.atan2(dy, dx);
    return { x: Math.cos(a) * d, y: Math.sin(a) * d };
  }, []);

  /** 设置一组瞳孔的朝向；不给 fx 时自动朝鼠标 */
  const setLook = useCallback(
    (pupils: (HTMLDivElement | null)[], fx?: number, fy?: number) => {
      pupils.forEach((p) => {
        if (!p) return;
        if (fx === undefined) {
          const o = pupilOffset(p);
          p.style.transform = `translate(${o.x}px,${o.y}px)`;
        } else {
          p.style.transform = `translate(${fx}px,${fy}px)`;
        }
      });
    },
    [pupilOffset],
  );

  /** 主渲染：所有状态分支的唯一定义处 */
  const render = useCallback(
    (s: CharState) => {
      const purple = purpleRef.current;
      const black = blackRef.current;
      const orange = orangeRef.current;
      const yellow = yellowRef.current;
      const eyesPurple = purpleEyesRef.current;
      const eyesBlack = blackEyesRef.current;
      const eyesOrange = orangeEyesRef.current;
      const eyesYellow = yellowEyesRef.current;
      const mouth = mouthRef.current;
      if (!purple || !black || !orange || !yellow) return;

      // passwordLength > 0 才参与「藏 / 露」判定：空密码框时角色不该有反应
      const isHiding = s.passwordLength > 0 && !s.showPassword;
      const revealed = s.passwordLength > 0 && s.showPassword;
      const excited = s.isTyping || isHiding;

      const pp = faceOf(purple);
      const bp = faceOf(black);
      const op = faceOf(orange);
      const yp = faceOf(yellow);

      // ── 身体 ──
      if (revealed) {
        purple.style.height = `${CANVAS_H}px`;
        purple.style.transform = 'translateX(0) skewX(0deg)';
      } else if (excited) {
        // 打字 / 藏密码：长高 40px 并前倾
        purple.style.height = `${CANVAS_H + 40}px`;
        purple.style.transform = `translateX(40px) skewX(${pp.bodySkew - 12}deg)`;
      } else {
        purple.style.height = `${CANVAS_H}px`;
        purple.style.transform = `translateX(0) skewX(${pp.bodySkew}deg)`;
      }

      if (revealed) {
        black.style.transform = 'translateX(0) skewX(0deg)';
      } else if (lookingAtEachOther.current) {
        black.style.transform = `translateX(20px) skewX(${bp.bodySkew * 1.5 + 10}deg)`;
      } else {
        black.style.transform = `translateX(0) skewX(${bp.bodySkew * (excited ? 1.5 : 1)}deg)`;
      }

      orange.style.transform = revealed ? 'skewX(0deg)' : `skewX(${op.bodySkew}deg)`;
      yellow.style.transform = revealed ? 'skewX(0deg)' : `skewX(${yp.bodySkew}deg)`;

      // ── 眼睛 ──
      if (eyesPurple) {
        if (revealed) {
          eyesPurple.style.left = '20px';
          eyesPurple.style.top = '35px';
          setLook(purplePupils.current, peeking.current ? 4 : -4, peeking.current ? 5 : -4);
        } else if (lookingAtEachOther.current) {
          eyesPurple.style.left = '55px';
          eyesPurple.style.top = '65px';
          setLook(purplePupils.current, 3, 4);
        } else {
          eyesPurple.style.left = `${45 + pp.faceX}px`;
          eyesPurple.style.top = `${40 + pp.faceY}px`;
          setLook(purplePupils.current);
        }
      }

      if (eyesBlack) {
        if (revealed) {
          eyesBlack.style.left = '10px';
          eyesBlack.style.top = '28px';
          setLook(blackPupils.current, -4, -4);
        } else if (lookingAtEachOther.current) {
          eyesBlack.style.left = '32px';
          eyesBlack.style.top = '12px';
          setLook(blackPupils.current, 0, -4);
        } else {
          eyesBlack.style.left = `${26 + bp.faceX}px`;
          eyesBlack.style.top = `${32 + bp.faceY}px`;
          setLook(blackPupils.current);
        }
      }

      if (eyesOrange) {
        if (revealed) {
          eyesOrange.style.left = '50px';
          eyesOrange.style.top = '85px';
          setLook(orangePupils.current, -5, -4);
        } else {
          eyesOrange.style.left = `${82 + op.faceX}px`;
          eyesOrange.style.top = `${90 + op.faceY}px`;
          setLook(orangePupils.current);
        }
      }

      if (eyesYellow) {
        if (revealed) {
          eyesYellow.style.left = '20px';
          eyesYellow.style.top = '35px';
          setLook(yellowPupils.current, -5, -4);
        } else {
          eyesYellow.style.left = `${52 + yp.faceX}px`;
          eyesYellow.style.top = `${40 + yp.faceY}px`;
          setLook(yellowPupils.current);
        }
      }

      if (mouth) {
        if (revealed) {
          mouth.style.left = '10px';
          mouth.style.top = '88px';
        } else {
          mouth.style.left = `${40 + yp.faceX}px`;
          mouth.style.top = `${88 + yp.faceY}px`;
        }
      }
    },
    [faceOf, setLook],
  );

  /** 合并到下一帧执行：1000Hz 鼠标每帧可派发十余次 mousemove */
  const scheduleRender = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      render(stateRef.current);
    });
  }, [render]);

  // ── 适配左栏可用空间（宽高同时夹取，避免矮屏溢出）
  // jsdom 无 ResizeObserver → 退回 scale=1；懒加载首帧由 RO 回调补齐
  useEffect(() => {
    const el = holderRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r || r.width <= 0 || r.height <= 0) return;
      setScale(Math.min(1.25, r.width / CANVAS_W, r.height / CANVAS_H));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ── 状态同步：props 变化时刷新镜像并重绘 ──
  useEffect(() => {
    stateRef.current = { isTyping, showPassword, passwordLength };
    render(stateRef.current);
  }, [isTyping, showPassword, passwordLength, render]);

  // ── 输入聚焦 → 紫黑对视 800ms（isTyping 上升沿触发）──
  useEffect(() => {
    lookingAtEachOther.current = isTyping;
    render(stateRef.current);
    if (!isTyping) return;
    const timer = setTimeout(() => {
      lookingAtEachOther.current = false;
      render(stateRef.current);
    }, 800);
    return () => clearTimeout(timer);
  }, [isTyping, render]);

  // ── 鼠标跟随（rAF 合并，全程 0 次 React 重渲染）──
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      mouse.current.x = e.clientX;
      mouse.current.y = e.clientY;
      scheduleRender();
    };
    window.addEventListener('mousemove', onMove, { passive: true });
    return () => {
      window.removeEventListener('mousemove', onMove);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [scheduleRender]);

  // ── 随机眨眼（紫 + 黑），间隔 3000–7000ms，每次闭眼 150ms ──
  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;

    // cancelled 标志必需：clearTimeout 拦不住「已在执行中」的回调，而这里是
    // 回调内部再 setTimeout 自续期 —— 少了标志位，卸载后定时器链会永久续下去
    let cancelled = false;
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const chains: Array<[() => (HTMLDivElement | null)[], number]> = [
      [() => purpleEyeballs.current, 18],
      [() => blackEyeballs.current, 16],
    ];

    const run = (getEls: () => (HTMLDivElement | null)[], rest: number) => {
      const tick = () => {
        const t1 = setTimeout(() => {
          timers.delete(t1);
          if (cancelled) return;
          const ebs = getEls().filter(Boolean) as HTMLDivElement[];
          ebs.forEach((eb) => { eb.style.height = '2px'; });
          const t2 = setTimeout(() => {
            timers.delete(t2);
            if (cancelled) return;
            ebs.forEach((eb) => { eb.style.height = `${rest}px`; });
            tick();
          }, 150);
          timers.add(t2);
        }, 3000 + Math.random() * 4000);
        timers.add(t1);
      };
      tick();
    };
    chains.forEach(([getEls, rest]) => run(getEls, rest));

    return () => {
      cancelled = true;
      timers.forEach(clearTimeout);
      timers.clear();
      // 若恰好停在闭眼帧必须复位，否则眼睛永久停在 2px
      chains.forEach(([getEls, rest]) => {
        (getEls().filter(Boolean) as HTMLDivElement[]).forEach((eb) => {
          eb.style.height = `${rest}px`;
        });
      });
    };
  }, []);

  // ── 密码可见且有内容时的「偷看」，间隔 2000–5000ms，每次 800ms ──
  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;

    let cancelled = false;
    const timers = new Set<ReturnType<typeof setTimeout>>();

    const schedule = () => {
      const t1 = setTimeout(() => {
        timers.delete(t1);
        if (cancelled) return;
        const s = stateRef.current;
        if (s.passwordLength > 0 && s.showPassword) {
          peeking.current = true;
          render(s);
          const t2 = setTimeout(() => {
            timers.delete(t2);
            if (cancelled) return;
            peeking.current = false;
            render(stateRef.current);
            schedule();
          }, 800);
          timers.add(t2);
        } else {
          schedule();
        }
      }, 2000 + Math.random() * 3000);
      timers.add(t1);
    };
    schedule();

    return () => {
      cancelled = true;
      timers.forEach(clearTimeout);
      timers.clear();
      peeking.current = false;
    };
  }, [render]);

  // ── 首帧：鼠标默认在视口中心，避免初始角度为 0 时眼睛呆滞 ──
  useEffect(() => {
    mouse.current.x = window.innerWidth / 2;
    mouse.current.y = window.innerHeight / 2;
    render(stateRef.current);
  }, [render]);

  /** 白眼球内嵌瞳孔 */
  const eyeball = (
    r: (el: HTMLDivElement | null) => void,
    p: (el: HTMLDivElement | null) => void,
    size: number,
    pupil: number,
  ) => (
    <div
      ref={r}
      style={{
        width: size, height: size, borderRadius: 999,
        background: '#fff', display: 'flex', alignItems: 'center',
        justifyContent: 'center', overflow: 'hidden',
        transition: 'height 150ms ease-out',
      }}
    >
      <div
        ref={p}
        style={{
          width: pupil, height: pupil, borderRadius: 999,
          background: CH_BLACK, transition: 'transform 100ms ease-out',
        }}
      />
    </div>
  );

  /** 裸瞳孔（橙 / 黄，没有白眼球） */
  const barePupil = (p: (el: HTMLDivElement | null) => void, size = 12) => (
    <div
      ref={p}
      style={{
        width: size, height: size, borderRadius: 999,
        background: CH_BLACK, transition: 'transform 100ms ease-out',
      }}
    />
  );

  return (
    <div ref={holderRef} className="relative h-full w-full min-h-[200px]">
      {/* 减少动效偏好：一并停掉所有过渡（眨眼 / 偷看定时器另外不启动） */}
      <style>{`
        @media (prefers-reduced-motion: reduce) {
          .auth-chars, .auth-chars * { transition-duration: 0.01ms !important; }
        }
      `}</style>

      {/* 按 550×400 绘制后整体缩放：绝对定位 + translateX(-50%) 让布局盒恒为 550px 宽，
          容器更窄时两侧对称溢出，视觉结果稳定居中（不依赖 flex 的溢出分配行为） */}
      <div
        className="auth-chars absolute bottom-0 left-1/2"
        style={{
          width: CANVAS_W, height: CANVAS_H,
          transform: `translateX(-50%) scale(${scale})`,
          transformOrigin: 'bottom center',
        }}
        aria-hidden="true"
      >
        {/* 紫色：最高，后排 */}
        <div
          ref={purpleRef}
          style={{
            position: 'absolute', bottom: 0, left: 70, width: 180, height: CANVAS_H,
            background: CH_PURPLE, borderRadius: '10px 10px 0 0', zIndex: 1,
            transformOrigin: 'bottom center', transition: BODY_TRANSITION,
          }}
        >
          <div ref={purpleEyesRef} style={{ position: 'absolute', display: 'flex', gap: 32, transition: EYES_TRANSITION }}>
            {eyeball((el) => { purpleEyeballs.current[0] = el; }, (el) => { purplePupils.current[0] = el; }, 18, 7)}
            {eyeball((el) => { purpleEyeballs.current[1] = el; }, (el) => { purplePupils.current[1] = el; }, 18, 7)}
          </div>
        </div>

        {/* 黑色：中排 */}
        <div
          ref={blackRef}
          style={{
            position: 'absolute', bottom: 0, left: 240, width: 120, height: 310,
            background: CH_BLACK, borderRadius: '8px 8px 0 0', zIndex: 2,
            transformOrigin: 'bottom center', transition: BODY_TRANSITION,
          }}
        >
          <div ref={blackEyesRef} style={{ position: 'absolute', display: 'flex', gap: 24, transition: EYES_TRANSITION }}>
            {eyeball((el) => { blackEyeballs.current[0] = el; }, (el) => { blackPupils.current[0] = el; }, 16, 6)}
            {eyeball((el) => { blackEyeballs.current[1] = el; }, (el) => { blackPupils.current[1] = el; }, 16, 6)}
          </div>
        </div>

        {/* 橙色：前排左，半圆（圆角 = 宽度一半） */}
        <div
          ref={orangeRef}
          style={{
            position: 'absolute', bottom: 0, left: 0, width: 240, height: 200,
            background: CH_ORANGE, borderRadius: '120px 120px 0 0', zIndex: 3,
            transformOrigin: 'bottom center', transition: BODY_TRANSITION,
          }}
        >
          <div ref={orangeEyesRef} style={{ position: 'absolute', display: 'flex', gap: 32, transition: EYES_TRANSITION_FAST }}>
            {barePupil((el) => { orangePupils.current[0] = el; })}
            {barePupil((el) => { orangePupils.current[1] = el; })}
          </div>
        </div>

        {/* 黄色：前排右，半圆 + 嘴 */}
        <div
          ref={yellowRef}
          style={{
            position: 'absolute', bottom: 0, left: 310, width: 140, height: 230,
            background: CH_YELLOW, borderRadius: '70px 70px 0 0', zIndex: 4,
            transformOrigin: 'bottom center', transition: BODY_TRANSITION,
          }}
        >
          <div ref={yellowEyesRef} style={{ position: 'absolute', display: 'flex', gap: 24, transition: EYES_TRANSITION_FAST }}>
            {barePupil((el) => { yellowPupils.current[0] = el; })}
            {barePupil((el) => { yellowPupils.current[1] = el; })}
          </div>
          <div
            ref={mouthRef}
            style={{
              position: 'absolute', width: 80, height: 4,
              background: CH_BLACK, borderRadius: 999,
              transition: 'left 200ms ease-out, top 200ms ease-out',
            }}
          />
        </div>
      </div>
    </div>
  );
}
