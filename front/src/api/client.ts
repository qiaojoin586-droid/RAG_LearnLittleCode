/**
 * Axios 实例配置
 * 
 * 功能：
 * 1. 统一超时、请求头配置
 * 2. 请求拦截：自动注入 JWT Bearer Token
 * 3. 响应拦截：401 自动 refresh → 重试，失败则完整登出跳转
 */

import axios from 'axios';
import type { AxiosError, InternalAxiosRequestConfig } from 'axios';
import { useUserStore } from '../stores/useUserStore';

/** 创建 Axios 实例 */
const client = axios.create({
  baseURL: '',           // 空 = 相对路径，开发时走 Vite proxy
  timeout: 30000,        // 30 秒超时
  headers: { 'Content-Type': 'application/json' },
});

/**
 * 请求拦截器 — 自动注入 JWT
 * 
 * 从 localStorage 读取 token（与 Zustand persist 双写），
 * 自动添加到 Authorization 请求头。
 */
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('jwt_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  // 注入设备标识请求头，供后端设备会话管理使用
  const deviceId = localStorage.getItem('device_id');
  if (deviceId) {
    config.headers['X-Device-Id'] = deviceId;
  }
  return config;
});

/** 是否正在刷新 Token */
let isRefreshing = false;
/** 等待刷新完成的请求队列 */
let pendingQueue: Array<{ resolve: (token: string) => void; reject: (error: unknown) => void }> = [];

/**
 * 响应拦截器 — 401 自动刷新 Token
 * 
 * 流程：
 * 1. 收到 401 → 检查是否有 refreshToken
 * 2. 有 → 尝试 refresh，成功后用新 token 重试原请求
 * 3. refresh 失败 → 完整登出（清 store + 清 localStorage + 跳转）
 * 4. 无 refreshToken → 直接登出
 */
client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    // 401 且不是 refresh 请求本身、且没重试过
    const code = (error.response?.data as { code?: number } | undefined)?.code;
    const tokenInvalid = code === 40101 || code === 40102;
    if (error.response?.status === 401 && tokenInvalid && originalRequest &&
        !originalRequest._retry && !originalRequest.url?.endsWith('/auth/refresh')) {
      const refreshToken = localStorage.getItem('jwt_refresh_token');

      // 无 refreshToken → 直接登出
      if (!refreshToken) {
        handleLogout();
        return Promise.reject(error);
      }

      // 已在刷新中 → 排队等待
      if (isRefreshing) {
        originalRequest._retry = true;
        return new Promise((resolve, reject) => {
          pendingQueue.push({ resolve: (newToken: string) => {
              originalRequest.headers.Authorization = `Bearer ${newToken}`;
              resolve(client(originalRequest));
            }, reject,
          });
        });
      }

      originalRequest._retry = true;
      isRefreshing = true;

      try {
        const deviceId = localStorage.getItem('device_id') || undefined;
        const res = await axios.post('/api/v1/auth/refresh', {
          refresh_token: refreshToken,
          ...(deviceId ? { device_id: deviceId } : {}),
        });
        const { access_token, refresh_token: newRefreshToken } = res.data.data;

        // 更新 store + localStorage
        useUserStore.getState().updateTokens(access_token, newRefreshToken);

        // 用新 token 重试原请求
        originalRequest.headers.Authorization = `Bearer ${access_token}`;

        // 通知队列中的请求
        pendingQueue.forEach((pending) => pending.resolve(access_token));
        pendingQueue = [];

        return client(originalRequest);
      } catch {
        // refresh 失败 → 彻底登出
        pendingQueue.forEach((pending) => pending.reject(error));
        pendingQueue = [];
        handleLogout();
        return Promise.reject(error);
      } finally {
        isRefreshing = false;
      }
    }

    return Promise.reject(error);
  }
);

/** 完整登出：清 store + 跳转登录页 */
function handleLogout() {
  useUserStore.getState().logout();
  if (window.location.pathname !== '/login') {
    window.location.href = '/login';
  }
}

export default client;
