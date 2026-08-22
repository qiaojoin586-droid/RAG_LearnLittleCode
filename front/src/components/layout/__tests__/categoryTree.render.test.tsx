/**
 * CategoryTree 挂载渲染测试（临时诊断用，验证树组件在空数据/正常数据下的渲染）
 *
 * 背景：用户反馈"前端显示的所有分类消失"，后端 API 返回 200 且数据完好，
 * 用 jsdom 挂载真实组件排查前端渲染层问题。
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

// jsdom 缺 ResizeObserver（dnd-kit 依赖），补 polyfill
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = globalThis.ResizeObserver ?? ResizeObserverMock;

vi.mock('../../../api/category', () => ({
  categoryApi: {
    getTree: vi.fn(),
    create: vi.fn(), update: vi.fn(), delete: vi.fn(), restore: vi.fn(),
    move: vi.fn(), reorder: vi.fn(), batch: vi.fn(), getRecycleBin: vi.fn(),
    permanentDelete: vi.fn(),
  },
}));

// i18n 初始化（CategoryTree 使用 useTranslation）
import '../../../i18n';

import CategoryTree from '../CategoryTree';
import { categoryApi } from '../../../api/category';
import { useCategoryStore } from '../../../stores/useCategoryStore';
import type { Category } from '../../../types/category';

function makeCategory(id: string, name: string, children: Category[] = []): Category {
  return {
    id, name, parent_id: null, icon: null, color: null, sort_order: 0,
    created_at: '2026-08-21T00:00:00', updated_at: '2026-08-21T00:00:00',
    children, note_count: children.length > 0 ? 1 : 0,
  };
}

const mockTree = {
  data: {
    data: {
      categories: [
        makeCategory('work', '工作', [
          makeCategory('script', '脚本'),
          makeCategory('docs', '项目文档', [makeCategory('xxx', 'XXX服务')]),
        ]),
        makeCategory('study', '学习'),
      ],
      uncategorized_count: 2,
    },
  },
};

describe('CategoryTree 渲染', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    useCategoryStore.setState({ categories: [], uncategorizedCount: 0, selectedCategoryId: null });
  });

  it('正常数据：树渲染所有分类名与数量徽标', async () => {
    vi.mocked(categoryApi.getTree).mockResolvedValue(mockTree as never);

    await act(async () => {
      root.render(<CategoryTree />);
      await new Promise((r) => setTimeout(r, 0)); // flush fetch
    });

    const text = container.textContent ?? '';
    expect(text).toContain('所有笔记');
    expect(text).toContain('未分类');
    expect(text).toContain('工作');
    expect(text).toContain('脚本');
    expect(text).toContain('项目文档');
    expect(text).toContain('XXX服务');
    expect(text).toContain('学习');
    console.log('[诊断] 正常数据渲染内容:', text.replace(/\s+/g, ' ').slice(0, 200));
  });

  it('空数据：仅渲染伪节点（所有笔记/未分类），不崩溃', async () => {
    vi.mocked(categoryApi.getTree).mockResolvedValue({
      data: { data: { categories: [], uncategorized_count: 0 } },
    } as never);

    await act(async () => {
      root.render(<CategoryTree />);
      await new Promise((r) => setTimeout(r, 0));
    });

    const text = container.textContent ?? '';
    expect(text).toContain('所有笔记');
    console.log('[诊断] 空数据渲染内容:', text.replace(/\s+/g, ' ').slice(0, 120));
  });

  it('API 报错：静默降级为伪节点，不崩溃', async () => {
    vi.mocked(categoryApi.getTree).mockRejectedValue(new Error('network'));

    await act(async () => {
      root.render(<CategoryTree />);
      await new Promise((r) => setTimeout(r, 0));
    });

    const text = container.textContent ?? '';
    expect(text).toContain('所有笔记');
    console.log('[诊断] 报错渲染内容:', text.replace(/\s+/g, ' ').slice(0, 120));
  });
});
