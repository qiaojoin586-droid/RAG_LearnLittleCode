/**
 * NoteList 整页挂载测试（临时诊断用）
 *
 * 背景：用户反馈"分类管理模块被破坏、前端显示的所有分类消失"，
 * 后端树接口 200、CategoryTree 单独挂载正常，此处排查 NoteList 页面层渲染。
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = globalThis.ResizeObserver ?? ResizeObserverMock;

// mock 依赖的 API 模块
vi.mock('../../api/category', () => ({
  categoryApi: {
    getTree: vi.fn().mockResolvedValue({
      data: {
        data: {
          categories: [
            {
              id: 'work', name: '工作', parent_id: null, icon: null, color: null,
              sort_order: 0, created_at: '', updated_at: '',
              children: [], note_count: 1,
            },
          ],
          uncategorized_count: 0,
        },
      },
    }),
    create: vi.fn(), update: vi.fn(), delete: vi.fn(), restore: vi.fn(),
    move: vi.fn(), reorder: vi.fn(), batch: vi.fn(), getRecycleBin: vi.fn(),
    permanentDelete: vi.fn(),
  },
}));
vi.mock('../../api/notes', () => ({
  notesApi: {
    list: vi.fn().mockResolvedValue({ data: { data: { notes: [], total: 0, page: 1, page_size: 20 } } }),
    detail: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn(),
    listDeleted: vi.fn(), restore: vi.fn(), permanentDelete: vi.fn(),
    search: vi.fn(), batch: vi.fn(), autocomplete: vi.fn(), writeAssistant: vi.fn(),
    moveToCategory: vi.fn(),
  },
}));

import '../../i18n';

import NoteList from '../NoteList';
import { useCategoryStore } from '../../stores/useCategoryStore';

describe('NoteList 页面渲染', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    useCategoryStore.setState({ categories: [], uncategorizedCount: 0, selectedCategoryId: null });
  });

  it('挂载不崩溃，树区域渲染分类', async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/notes']}>
          <Routes>
            <Route path="/notes" element={<NoteList />} />
          </Routes>
        </MemoryRouter>
      );
      await new Promise((r) => setTimeout(r, 50)); // flush 异步
    });

    const text = container.textContent ?? '';
    console.log('[诊断] NoteList 渲染内容:', text.replace(/\s+/g, ' ').slice(0, 300));
    expect(text).toContain('工作');
    expect(text).toContain('所有笔记');
  });
});
