/**
 * 主布局（v2 新壳层）
 *
 * TopBar + IconRail + SidePanel + 内容区 + AI 对话浮层。
 * 包含路由守卫：未登录 → 重定向到 /login。
 *
 * v2 变更（对比旧两栏布局）：
 * - 旧 Sidebar 替换为 IconRail（56px）+ SidePanel（280px 可折叠）
 * - /notes 路由下 SidePanel 强制折叠（笔记页自带三栏，阶段三实现）
 * - AI 对话唯一化为右侧浮层（阶段二迁入完整功能，当前为骨架）
 * - 浮动装饰块弱化（透明度由 CSS 变量控制）
 * - 内容区去除 p-6 max-w-6xl 居中限制
 *
 * z-index 层级体系：TopBar(100) > AI 浮层(90) > 移动端遮罩(80) > SidePanel(70) > IconRail(60) > 内容区(10)
 */

import { lazy, Suspense, useEffect, useState } from 'react';
import { Outlet, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useUserStore } from '../stores/useUserStore';
import TopBar from '../components/layout/TopBar';
import IconRail, { type PanelType } from '../components/layout/IconRail';
import SidePanel from '../components/layout/SidePanel';
import type { AssistantView } from '../components/layout/AIAssistantPanel';

// AI 浮层懒加载：内部含 markdown/katex 等重型依赖，首次打开时才加载，
// 避免进入首屏包（v2 阶段二性能修复）
const AIAssistantPanel = lazy(() => import('../components/layout/AIAssistantPanel'));

// 全局搜索模态框懒加载（阶段四）
const GlobalSearchModal = lazy(() => import('../components/layout/GlobalSearchModal'));

// 路由 → 面板类型映射
const routeToPanel: Record<string, PanelType> = {
  '/notes': 'notes',
  '/review': 'review',
  '/knowledge': 'knowledge',
  '/ppt-templates': 'ppt',
  '/recycle-bin': 'recycle-bin',
};

// 面板类型 → 默认路由
const panelToRoute: Record<PanelType, string> = {
  notes: '/notes',
  review: '/review',
  knowledge: '/knowledge',
  ppt: '/ppt-templates',
  'recycle-bin': '/recycle-bin',
};

export default function MainLayout() {
  const isLogin = useUserStore((s) => s.isLogin);
  const location = useLocation();
  const navigate = useNavigate();

  // 面板与浮层状态
  const [activePanel, setActivePanel] = useState<PanelType | null>(null);
  const [panelVisible, setPanelVisible] = useState(false);
  const [assistantVisible, setAssistantVisible] = useState(false);
  const [assistantView, setAssistantView] = useState<AssistantView>('chat');
  // 浮层首次打开后保持挂载（关闭仅平移隐藏，流式/消息状态跨开合保留）
  const [assistantMounted, setAssistantMounted] = useState(false);
  const [globalSearchOpen, setGlobalSearchOpen] = useState(false);

  // 监听路由变化，自动同步面板状态
  useEffect(() => {
    const pathname = location.pathname;
    const matchedPanel = Object.entries(routeToPanel).find(
      ([route]) => pathname === route || pathname.startsWith(route + '/')
    )?.[1] as PanelType | undefined;

    if (matchedPanel && matchedPanel !== activePanel) {
      setActivePanel(matchedPanel);
      // /notes 下 SidePanel 强制折叠（笔记页自带三栏）
      setPanelVisible(matchedPanel !== 'notes');
    }
  }, [location.pathname, activePanel]);

  // 处理图标栏点击
  const handlePanelChange = (panel: PanelType) => {
    if (panel === activePanel) {
      // 再次点击已激活图标 → 折叠/展开面板；/notes 始终折叠
      // 同时回到分区根路由（如编辑器内点击笔记图标返回笔记列表）
      setPanelVisible(panel === 'notes' ? false : !panelVisible);
      navigate(panelToRoute[panel]);
    } else {
      setActivePanel(panel);
      setPanelVisible(panel !== 'notes');
      navigate(panelToRoute[panel]);
    }
  };

  // 打开 AI 浮层（对话/历史视图）；首次打开触发懒加载挂载
  const handleOpenAssistant = (view: AssistantView) => {
    setAssistantView(view);
    setAssistantVisible(true);
    setAssistantMounted(true);
  };

  // 图标栏开关语义（用户需求）：同视图再次点击关闭；不同视图切换打开
  // 全局搜索的会话结果仍走 handleOpenAssistant（总是打开，避免误关）
  const handleToggleAssistant = (view: AssistantView) => {
    if (assistantVisible && assistantView === view) {
      setAssistantVisible(false);
    } else {
      handleOpenAssistant(view);
    }
  };

  // 全局快捷键：Cmd/Ctrl+K 打开搜索，Escape 关闭弹层
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setGlobalSearchOpen(true);
      }
      if (e.key === 'Escape') {
        setGlobalSearchOpen(false);
        setAssistantVisible(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // 路由守卫：未登录 → 重定向到登录页
  if (!isLogin) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="flex flex-col h-screen relative overflow-hidden"
      style={{ background: 'var(--color-bg)' }}>

      {/* 顶部导航栏 */}
      <TopBar
        assistantVisible={assistantVisible}
        onToggleAssistant={() => {
          const next = !assistantVisible;
          setAssistantVisible(next);
          if (next) setAssistantMounted(true);
        }}
        onGlobalSearch={() => setGlobalSearchOpen(true)}
      />

      {/* 低透明度浮动圆角矩形装饰（弱化：透明度由 --color-decor-* 控制） */}
      <div className="absolute inset-0 pointer-events-none overflow-hidden" aria-hidden="true">
        <div className="absolute -top-16 -right-20 w-72 h-72 rounded-[40px] rotate-12"
          style={{ background: 'var(--color-decor-1)' }} />
        <div className="absolute top-1/3 -left-16 w-56 h-56 rounded-[32px] -rotate-6"
          style={{ background: 'var(--color-decor-2)' }} />
        <div className="absolute -bottom-20 right-1/4 w-64 h-64 rounded-[36px] rotate-45"
          style={{ background: 'var(--color-decor-3)' }} />
      </div>

      {/* 下方主体区域（TopBar 以下） */}
      <div className="flex flex-1 overflow-hidden relative" style={{ zIndex: 1 }}>

        {/* 窄图标栏 */}
        <IconRail
          activePanel={activePanel}
          onPanelChange={handlePanelChange}
          onToggleAssistant={handleToggleAssistant}
        />

        {/* 弹出式面板容器（/notes 下强制折叠）
            移动端：fixed 抽屉（top-14 left-14 从顶栏/图标栏下缘开始）；
            桌面端：md:inset-auto 重置偏移 + md:relative 回内联流，
            否则 left-14/top-14 会作为 relative 偏移把面板压进主内容区（知识库布局 BUG 根因） */}
        <div
          className={`transition-all duration-300 ease-in-out overflow-hidden border-r border-[var(--color-panel-border)] shrink-0 ${
            panelVisible ? 'w-[280px]' : 'w-0'
          } fixed top-14 left-14 bottom-0 z-50 md:relative md:inset-auto md:z-auto md:translate-x-0 ${
            !panelVisible && 'md:w-0'
          } ${panelVisible ? 'md:w-[280px] translate-x-0' : '-translate-x-full md:translate-x-0'}`}
          style={{
            background: 'var(--color-sidebar-bg)',
            backdropFilter: 'blur(12px)',
            WebkitBackdropFilter: 'blur(12px)',
          }}
        >
          <SidePanel
            activePanel={activePanel}
            onClose={() => setPanelVisible(false)}
          />
        </div>

        {/* 移动端遮罩层 */}
        {panelVisible && (
          <div
            className="fixed inset-0 bg-black/20 z-40 md:hidden"
            onClick={() => setPanelVisible(false)}
          />
        )}

        {/* 内容区 */}
        <main className="flex-1 overflow-y-auto relative">
          <Outlet />
        </main>

        {/* AI 对话浮层（懒加载：首次打开后才挂载，挂载后常驻） */}
        {assistantMounted && (
          <Suspense fallback={null}>
            <AIAssistantPanel
              isVisible={assistantVisible}
              view={assistantView}
              onViewChange={setAssistantView}
              onClose={() => setAssistantVisible(false)}
            />
          </Suspense>
        )}
      </div>

      {/* 全局搜索模态框（Cmd/Ctrl+K，前端聚合检索） */}
      {globalSearchOpen && (
        <Suspense fallback={null}>
          <GlobalSearchModal
            onClose={() => setGlobalSearchOpen(false)}
            onOpenAssistant={handleOpenAssistant}
          />
        </Suspense>
      )}
    </div>
  );
}
