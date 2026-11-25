// @vitest-environment jsdom
/**
 * Axios client 拦截器测试（审查 P1-2 前端测试网）
 *
 * 覆盖：
 * - 请求拦截器：自动注入 Bearer token 与设备标识头
 * - 响应拦截器：401 → 单飞刷新 token → 重试原请求（排队请求共享新 token）
 * - 无 refreshToken 或刷新失败 → 完整登出
 *
 * 通过 vi.mock('axios') 捕获实例拦截器注册的回调，直接驱动业务逻辑。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

/** 拦截器回调与 store 的共享状态（vi.hoisted 保证 mock 工厂内可访问） */
const state = vi.hoisted(() => ({
  requestHandler: undefined as unknown,
  responseOk: undefined as unknown,
  responseErr: undefined as unknown,
  refreshResolve: undefined as ((v: unknown) => void) | undefined,
  store: { updateTokens: vi.fn(), logout: vi.fn() },
}));

/** 模拟 axios 实例（可调用 → 重试请求的返回） */
const instanceMock = vi.hoisted(() => vi.fn(async () => ({ data: { data: { ok: true } } })));
const postMock = vi.hoisted(() =>
  vi.fn(() => new Promise((resolve) => { state.refreshResolve = resolve; })),
);

vi.mock('axios', () => {
  const instance = Object.assign(instanceMock, {
    interceptors: {
      request: { use: (fn: unknown) => { state.requestHandler = fn; } },
      response: { use: (ok: unknown, err: unknown) => { state.responseOk = ok; state.responseErr = err; } },
    },
    defaults: {},
  });
  return {
    default: {
      create: () => instance,
      post: postMock,
      isAxiosError: (e: unknown) => Boolean(e && typeof e === 'object' && 'isAxiosError' in e),
    },
  };
});

vi.mock('../../stores/useUserStore', () => ({
  useUserStore: { getState: () => state.store },
}));

import client from '../client';

describe('client 模块', () => {
  it('导出 axios 实例（副作用导入：注册拦截器）', () => {
    expect(client).toBeDefined();
  });
});

describe('client 请求拦截器', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('自动注入 Bearer token 与设备标识头', () => {
    localStorage.setItem('jwt_token', 'access-1');
    localStorage.setItem('device_id', 'dev-1');
    const config = { headers: {} as Record<string, string> };
    (state.requestHandler as (c: typeof config) => typeof config)(config);
    expect(config.headers.Authorization).toBe('Bearer access-1');
    expect(config.headers['X-Device-Id']).toBe('dev-1');
  });

  it('无 token 时不注入 Authorization', () => {
    const config = { headers: {} as Record<string, string> };
    (state.requestHandler as (c: typeof config) => typeof config)(config);
    expect(config.headers.Authorization).toBeUndefined();
  });
});

describe('client 响应拦截器（401 刷新）', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    state.refreshResolve = undefined;
  });

  const make401 = (url = '/api/v1/chat/query') => ({
    response: { status: 401, data: { code: 40101 } },
    config: { headers: {} as Record<string, string>, url },
    isAxiosError: true,
  });

  it('401 → 刷新 token → 重试原请求（带新 token）', async () => {
    localStorage.setItem('jwt_token', 'old-access');
    localStorage.setItem('jwt_refresh_token', 'old-refresh');
    localStorage.setItem('device_id', 'dev-1');

    const error = make401();
    const resultPromise = (state.responseErr as (e: unknown) => Promise<unknown>)(error);

    // 触发 refresh 请求
    expect(postMock).toHaveBeenCalledWith('/api/v1/auth/refresh', expect.objectContaining({
      refresh_token: 'old-refresh',
      device_id: 'dev-1',
    }));

    // 完成刷新
    state.refreshResolve?.({ data: { data: { access_token: 'new-access', refresh_token: 'new-refresh' } } });

    const result = await resultPromise;
    expect(result).toEqual({ data: { data: { ok: true } } }); // 重试成功
    expect(state.store.updateTokens).toHaveBeenCalledWith('new-access', 'new-refresh');
    // 重试请求使用新 token
    expect(instanceMock).toHaveBeenCalledWith(expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer new-access' }),
    }));
  });

  it('刷新进行中：后续 401 排队共享新 token（单飞）', async () => {
    localStorage.setItem('jwt_token', 'old');
    localStorage.setItem('jwt_refresh_token', 'old-refresh');

    const p1 = (state.responseErr as (e: unknown) => Promise<unknown>)(make401('/api/v1/a'));
    const p2 = (state.responseErr as (e: unknown) => Promise<unknown>)(make401('/api/v1/b'));

    expect(postMock).toHaveBeenCalledTimes(1); // 单飞：只发一次 refresh

    state.refreshResolve?.({ data: { data: { access_token: 'new-access', refresh_token: 'nr' } } });

    await Promise.all([p1, p2]);
    expect(instanceMock).toHaveBeenCalledTimes(2); // 两个请求都用新 token 重试
  });

  it('无 refreshToken → 直接登出', async () => {
    localStorage.setItem('jwt_token', 'old-access');

    const error = make401();
    await expect((state.responseErr as (e: unknown) => Promise<unknown>)(error)).rejects.toBe(error);
    expect(state.store.logout).toHaveBeenCalledTimes(1);
    expect(postMock).not.toHaveBeenCalled();
  });

  it('刷新失败 → 登出并拒绝原错误', async () => {
    localStorage.setItem('jwt_refresh_token', 'old-refresh');
    postMock.mockRejectedValueOnce(new Error('refresh failed'));

    const error = make401();
    await expect((state.responseErr as (e: unknown) => Promise<unknown>)(error)).rejects.toBe(error);
    expect(state.store.logout).toHaveBeenCalledTimes(1);
  });

  it('非 401 错误 → 透传不登出', async () => {
    const error = { response: { status: 500 }, config: { headers: {} }, isAxiosError: true };
    await expect((state.responseErr as (e: unknown) => Promise<unknown>)(error)).rejects.toBe(error);
    expect(state.store.logout).not.toHaveBeenCalled();
  });

  it.each([40110, 40111, 40103])('业务认证错误 %s 不刷新或登出', async (code) => {
    localStorage.setItem('jwt_refresh_token', 'refresh');
    const error = make401();
    error.response.data.code = code;
    await expect((state.responseErr as (e: unknown) => Promise<unknown>)(error)).rejects.toBe(error);
    expect(postMock).not.toHaveBeenCalled();
    expect(state.store.logout).not.toHaveBeenCalled();
  });

  it('刷新失败会拒绝所有排队请求，不遗留等待', async () => {
    localStorage.setItem('jwt_refresh_token', 'refresh');
    postMock.mockRejectedValueOnce(new Error('refresh failed'));
    const handler = state.responseErr as (e: unknown) => Promise<unknown>;
    const results = await Promise.allSettled([handler(make401('/a')), handler(make401('/b'))]);
    expect(results.map((result) => result.status)).toEqual(['rejected', 'rejected']);
    expect(postMock).toHaveBeenCalledTimes(1);
  });
});
