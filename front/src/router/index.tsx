/**
 * 路由配置
 *
 * 两种布局：
 * - AuthLayout: /login, /register（不需要登录）
 * - MainLayout: 其余页面（需要登录，含路由守卫）
 *
 * 所有页面组件使用 React.lazy() 懒加载。
 *
 * v2 变更：AI 对话唯一化为浮层后，/chat、/chat/:sessionId、/sessions
 * 已删除并重定向到 /notes（AIChat.tsx / Sessions.tsx 标记 deprecated，阶段五删除）。
 */

import { lazy, Suspense } from 'react';
import type { RouteObject } from 'react-router-dom';
import { Navigate } from 'react-router-dom';
import AuthLayout from '../layouts/AuthLayout';
import MainLayout from '../layouts/MainLayout';
import LoadingSkeleton from '../components/common/LoadingSkeleton';

// 懒加载页面组件
const Login = lazy(() => import('../pages/Login'));
const Register = lazy(() => import('../pages/Register'));
const NoteList = lazy(() => import('../pages/NoteList'));
const KnowledgeBase = lazy(() => import('../pages/KnowledgeBase'));
const DailyReview = lazy(() => import('../pages/DailyReview'));
const RecycleBin = lazy(() => import('../pages/RecycleBin'));
const Profile = lazy(() => import('../pages/Profile'));
const Settings = lazy(() => import('../pages/Settings'));
const AboutUs = lazy(() => import('../pages/AboutUs'));
const PPTTemplates = lazy(() => import('../pages/PPTTemplates'));

/** 包装 Suspense */
function LazyPage({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={<LoadingSkeleton />}>
      {children}
    </Suspense>
  );
}

/** 路由配置 */
export const routes: RouteObject[] = [
  // 认证页面（AuthLayout）
  {
    element: <AuthLayout />,
    children: [
      {
        path: '/login',
        element: <LazyPage><Login /></LazyPage>,
      },
      {
        path: '/register',
        element: <LazyPage><Register /></LazyPage>,
      },
    ],
  },
  // 主应用（MainLayout，需要登录）
  {
    element: <MainLayout />,
    children: [
      {
        path: '/',
        element: <LazyPage><NoteList /></LazyPage>,
      },
      {
        path: '/notes',
        element: <LazyPage><NoteList /></LazyPage>,
      },
      // v2：笔记详情内联化 —— /notes/new、/notes/:id 均渲染三栏页
      //（NoteList 通过 URL 参数同步右侧详情区选中态，无全屏跳转）
      {
        path: '/notes/new',
        element: <LazyPage><NoteList /></LazyPage>,
      },
      {
        path: '/notes/:id',
        element: <LazyPage><NoteList /></LazyPage>,
      },
      // v2：旧 AI 路由重定向到 /notes（AI 对话已唯一化为浮层）
      {
        path: '/chat',
        element: <Navigate to="/notes" replace />,
      },
      {
        path: '/chat/:sessionId',
        element: <Navigate to="/notes" replace />,
      },
      {
        path: '/sessions',
        element: <Navigate to="/notes" replace />,
      },
      {
        path: '/knowledge',
        element: <LazyPage><KnowledgeBase /></LazyPage>,
      },
      {
        path: '/review',
        element: <LazyPage><DailyReview /></LazyPage>,
      },
      {
        path: '/recycle-bin',
        element: <LazyPage><RecycleBin /></LazyPage>,
      },
      {
        path: '/profile',
        element: <LazyPage><Profile /></LazyPage>,
      },
      {
        path: '/settings',
        element: <LazyPage><Settings /></LazyPage>,
      },
      {
        path: '/about',
        element: <LazyPage><AboutUs /></LazyPage>,
      },
      {
        path: '/ppt-templates',
        element: <LazyPage><PPTTemplates /></LazyPage>,
      },
    ],
  },
];
