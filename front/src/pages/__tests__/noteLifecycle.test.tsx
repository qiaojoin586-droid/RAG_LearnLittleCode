// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import type { AxiosResponse } from 'axios';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('../../api/notes', () => ({ notesApi: { list: vi.fn(), update: vi.fn() } }));
vi.mock('../../components/layout/CategoryTree', () => ({ default: () => null }));
vi.mock('../../components/layout/NoteCardItem', () => ({
  default: ({ note }: { note: { id: string; title: string } }) => <div data-note={note.id}>{note.title}</div>,
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import '../../i18n';
import NoteList from '../NoteList';
import NoteEditor from '../NoteEditor';
import { notesApi } from '../../api/notes';
import { useCategoryStore } from '../../stores/useCategoryStore';
import type { ApiResponse, Note } from '../../types/api';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement;
let root: Root;

function response<T>(data: T): AxiosResponse<ApiResponse<T>> {
  return { data: { code: 0, message: 'ok', data, request_id: 'test' } } as AxiosResponse<ApiResponse<T>>;
}

function note(id: string, content = 'body', format = 'md'): Note {
  return { id, user_id: 'user', title: id, content, format, tags: [], is_pinned: false,
    category: null, category_id: null, created_at: '2026-10-25T00:00:00Z', updated_at: '2026-10-25T00:00:00Z' };
}

beforeEach(() => {
  vi.resetAllMocks();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  useCategoryStore.setState({ categories: [], selectedCategoryId: null });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

function loadMoreButton() {
  return [...container.querySelectorAll('button')].find(button => button.textContent?.includes('加载更多'));
}

function mockPages(total: number) {
  vi.mocked(notesApi.list).mockImplementation(async (params) => {
    const page = params?.page ?? 1;
    const start = (page - 1) * 20;
    return response({ notes: Array.from({ length: Math.min(20, total - start) },
      (_, i) => note(`${params?.category_id ?? 'all'}-${start + i}`)), total, page, page_size: 20 });
  });
}

it('loads all 30 notes without repeating page 1 or hiding the final page', async () => {
  mockPages(30);
  await act(async () => root.render(<MemoryRouter><NoteList /></MemoryRouter>));
  expect(loadMoreButton()).toBeDefined();
  await act(async () => loadMoreButton()!.click());
  expect(vi.mocked(notesApi.list).mock.calls.map(call => call[0]?.page)).toEqual([1, 2]);
  const ids = [...container.querySelectorAll('[data-note]')].map(card => card.getAttribute('data-note'));
  expect(ids).toHaveLength(30);
  expect(new Set(ids).size).toBe(30);
  expect(loadMoreButton()).toBeUndefined();
});

it('starts pagination over when changing categories after loading multiple pages', async () => {
  mockPages(65);
  await act(async () => root.render(<MemoryRouter><NoteList /></MemoryRouter>));
  await act(async () => loadMoreButton()!.click());
  await act(async () => loadMoreButton()!.click());
  await act(async () => useCategoryStore.getState().selectCategory('other'));
  await act(async () => loadMoreButton()!.click());
  expect(vi.mocked(notesApi.list).mock.calls.map(call => call[0]?.page)).toEqual([1, 2, 3, 1, 2]);
  const cards = [...container.querySelectorAll('[data-note]')];
  expect(cards).toHaveLength(40);
  expect(cards.every(card => card.getAttribute('data-note')?.startsWith('other-'))).toBe(true);
});

it.each([
  '<p>Hello <strong>world</strong></p>',
  '```html\n<p>literal source</p>\n```',
  '  plain text\r\nwith line endings  ',
])('preserves TXT source when saving without edits: %s', async (raw) => {
  const existing = note('source.txt', raw, 'txt');
  vi.mocked(notesApi.update).mockResolvedValue(response(existing));
  await act(async () => root.render(<NoteEditor note={existing} onCreated={vi.fn()} onUpdated={vi.fn()} />));
  const save = [...container.querySelectorAll('button')].find(button => button.textContent?.includes('保存'));
  await act(async () => save!.click());
  expect(vi.mocked(notesApi.update).mock.calls[0][1].content).toBe(raw);
  expect(vi.mocked(notesApi.update).mock.calls[0][1]).not.toHaveProperty('is_indexed');
});

it('continues converting legacy HTML in Markdown notes', async () => {
  const existing = note('legacy.md', '<p>Hello <strong>world</strong></p>');
  vi.mocked(notesApi.update).mockResolvedValue(response(existing));
  await act(async () => root.render(<NoteEditor note={existing} onCreated={vi.fn()} onUpdated={vi.fn()} />));
  const save = [...container.querySelectorAll('button')].find(button => button.textContent?.includes('保存'));
  await act(async () => save!.click());
  expect(vi.mocked(notesApi.update).mock.calls[0][1].content).toBe('Hello **world**');
});

it('saves an explicit change to AI search eligibility', async () => {
  const existing = { ...note('indexed'), is_indexed: true };
  vi.mocked(notesApi.update).mockResolvedValue(response({ ...existing, is_indexed: false }));
  const onUpdated = vi.fn();
  await act(async () => root.render(<NoteEditor note={existing} onCreated={vi.fn()} onUpdated={onUpdated} />));
  const option = [...container.querySelectorAll('input[type="checkbox"]')].find(
    input => (input as HTMLInputElement).labels?.[0]?.textContent?.includes('供 AI 检索'),
  ) as HTMLInputElement;
  expect(option.checked).toBe(true);
  await act(async () => option.click());
  const save = [...container.querySelectorAll('button')].find(button => button.textContent?.includes('保存'));
  await act(async () => save!.click());
  expect(notesApi.update).toHaveBeenCalledWith(existing.id, expect.objectContaining({ is_indexed: false }));
  expect(onUpdated).toHaveBeenCalledWith(expect.objectContaining({ is_indexed: false }));
});
