/**
 * 回收站页面
 *
 * 功能：
 * 1. 展示已删除笔记列表（标题、删除时间、距离自动清理的剩余天数）
 * 2. 单条恢复 / 单条彻底删除（双重确认，彻底删除不可恢复）
 * 3. 批量恢复 / 批量彻底删除（勾选模式）
 * 4. 剩余天数标签：绿 >7 天 / 黄 3-7 天 / 红 <3 天
 * 5. 「分类」tab：已软删除分类列表 + 恢复（级联恢复祖先链，见设计方案 §4.7）
 *
 * 注意：彻底删除会物理删除笔记及其回顾记录（级联），无法恢复。
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  Trash2, RotateCcw, CheckSquare, Square, Inbox, ChevronLeft, ChevronRight, Folder,
} from 'lucide-react';
import { notesApi } from '../api/notes';
import { categoryApi } from '../api/category';
import ConfirmDialog from '../components/common/ConfirmDialog';
import EmptyState from '../components/common/EmptyState';
import type { DeletedNote } from '../types/api';
import type { DeletedCategory } from '../types/category';

const PAGE_SIZE = 20;

/** 剩余天数 → 标签配色（绿 >7 / 黄 3-7 / 红 <3） */
function daysBadgeClass(days: number): string {
  if (days > 7) return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400';
  if (days >= 3) return 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400';
  return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400';
}

export default function RecycleBin() {
  // tab 切换：笔记 / 分类
  const [tab, setTab] = useState<'notes' | 'categories'>('notes');

  const [notes, setNotes] = useState<DeletedNote[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  // 分类回收站状态
  const [categories, setCategories] = useState<DeletedCategory[]>([]);
  const [catsLoading, setCatsLoading] = useState(false);
  // 分类 tab 独立勾选状态（与笔记 tab 隔离，避免串数据）
  const [catSelectMode, setCatSelectMode] = useState(false);
  const [catSelectedIds, setCatSelectedIds] = useState<Set<string>>(new Set());
  const [catSingleConfirm, setCatSingleConfirm] = useState<null | { type: 'restore' | 'permanent'; cat: DeletedCategory }>(null);
  const [catBatchConfirm, setCatBatchConfirm] = useState<null | 'restore' | 'permanent'>(null);

  // 确认弹窗状态
  const [singleConfirm, setSingleConfirm] = useState<null | { type: 'restore' | 'permanent'; note: DeletedNote }>(null);
  const [batchConfirm, setBatchConfirm] = useState<null | 'restore' | 'permanent'>(null);

  /** 加载回收站分类列表 */
  const fetchCategories = async () => {
    setCatsLoading(true);
    try {
      const res = await categoryApi.getRecycleBin();
      setCategories(res.data.data.categories ?? []);
    } catch {
      toast.error('加载分类回收站失败');
    } finally {
      setCatsLoading(false);
    }
  };

  // 首次进入"分类"tab 时加载（后续每次切换刷新）
  useEffect(() => {
    if (tab === 'categories') fetchCategories();
  }, [tab]);

  /** 恢复分类（级联恢复祖先链） */
  const handleRestoreCategory = async (cat: DeletedCategory) => {
    setBusy(true);
    try {
      await categoryApi.restore(cat.id);
      toast.success(`已恢复分类「${cat.name}」`);
      fetchCategories();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '恢复失败');
    } finally {
      setBusy(false);
    }
  };

  /** 彻底删除分类（物理删除，不可恢复；软删除态的子孙一并清除） */
  const handlePermanentDeleteCategory = async (cat: DeletedCategory) => {
    setBusy(true);
    try {
      await categoryApi.permanentDelete(cat.id);
      toast.success(`已彻底删除分类「${cat.name}」`);
      fetchCategories();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '彻底删除失败');
    } finally {
      setBusy(false);
    }
  };

  /** 分类批量操作（恢复 / 彻底删除） */
  const handleCatBatch = async (operation: 'restore' | 'permanent') => {
    const ids = [...catSelectedIds];
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const res = await categoryApi.batch({
        category_ids: ids,
        operation: operation === 'restore' ? 'restore' : 'permanent_delete',
      });
      const data = res.data.data as unknown as { success_count: number; error_count: number };
      toast.success(
        operation === 'restore'
          ? `成功恢复 ${data.success_count ?? ids.length} 个分类`
          : `已彻底删除 ${data.success_count ?? ids.length} 个分类`
      );
      setCatSelectedIds(new Set());
      setCatSelectMode(false);
      fetchCategories();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '批量操作失败');
    } finally {
      setBusy(false);
    }
  };

  /** 分类勾选切换 */
  const toggleCatSelect = (id: string) => {
    setCatSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  /** 分类全选 / 取消全选 */
  const toggleCatSelectAll = () => {
    if (categories.length > 0 && catSelectedIds.size === categories.length) {
      setCatSelectedIds(new Set());
    } else {
      setCatSelectedIds(new Set(categories.map((c) => c.id)));
    }
  };

  /** 加载回收站列表 */
  const fetchList = async (targetPage = page) => {
    setLoading(true);
    try {
      const res = await notesApi.listDeleted({ page: targetPage, page_size: PAGE_SIZE });
      setNotes(res.data.data.notes);
      setTotal(res.data.data.total);
      setPage(targetPage);
    } catch {
      toast.error('加载回收站失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchList(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 刷新并退出勾选模式 */
  const refresh = () => {
    setSelectedIds(new Set());
    fetchList();
  };

  /** 单条恢复 */
  const handleRestore = async (note: DeletedNote) => {
    setBusy(true);
    try {
      await notesApi.restore(note.id);
      toast.success(`已恢复「${note.title}」`);
      refresh();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '恢复失败');
    } finally {
      setBusy(false);
    }
  };

  /** 单条彻底删除 */
  const handlePermanentDelete = async (note: DeletedNote) => {
    setBusy(true);
    try {
      await notesApi.permanentDelete(note.id);
      toast.success(`已彻底删除「${note.title}」`);
      refresh();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '彻底删除失败');
    } finally {
      setBusy(false);
    }
  };

  /** 批量操作（恢复 / 彻底删除） */
  const handleBatch = async (operation: 'restore' | 'permanent') => {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const res = await notesApi.batch({
        note_ids: ids,
        operation: operation === 'restore' ? 'restore' : 'permanent_delete',
      });
      const { success_count } = res.data.data as unknown as {
        success_count: number;
        error_count: number;
      };
      toast.success(
        operation === 'restore'
          ? `成功恢复 ${success_count} 条笔记`
          : `已彻底删除 ${success_count} 条笔记`
      );
      refresh();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { message?: string } } };
      toast.error(error.response?.data?.message || '批量操作失败');
    } finally {
      setBusy(false);
    }
  };

  /** 勾选切换 */
  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  /** 全选 / 取消全选 */
  const toggleSelectAll = () => {
    if (notes.length > 0 && selectedIds.size === notes.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(notes.map((n) => n.id)));
    }
  };

  /** 剩余天数文案 */
  const daysLabel = (note: DeletedNote) =>
    note.days_remaining <= 0 ? '即将自动清理' : `剩余 ${note.days_remaining} 天`;

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="max-w-3xl mx-auto p-6">
      {/* 页头 */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-heading font-bold text-[var(--color-text)]">回收站</h1>
        {total > 0 && (
          <button
            onClick={() => {
              setSelectMode(!selectMode);
              setSelectedIds(new Set());
            }}
            className="flex items-center gap-2 px-4 py-2 rounded-[var(--radius-md)] border border-[var(--color-border)] text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-accent)] hover:border-[var(--color-accent)] transition-colors"
          >
            {selectMode ? <RotateCcw size={15} /> : <CheckSquare size={15} />}
            {selectMode ? '退出批量管理' : '批量管理'}
          </button>
        )}
      </div>

      {/* tab 切换：笔记 / 分类（分类管理功能新增） */}
      <div className="flex items-center gap-1 mb-4 p-1 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)] w-fit">
        <button
          onClick={() => setTab('notes')}
          className={`px-4 py-1.5 text-sm rounded-md transition-colors ${
            tab === 'notes'
              ? 'bg-[var(--color-accent)] text-white'
              : 'text-[var(--color-text-secondary)] hover:text-[var(--color-accent)]'
          }`}
        >
          笔记
        </button>
        <button
          onClick={() => setTab('categories')}
          className={`px-4 py-1.5 text-sm rounded-md transition-colors ${
            tab === 'categories'
              ? 'bg-[var(--color-accent)] text-white'
              : 'text-[var(--color-text-secondary)] hover:text-[var(--color-accent)]'
          }`}
        >
          分类
        </button>
      </div>

      {tab === 'notes' ? (
        <>
      {/* 批量操作栏 */}
      {selectMode && (
        <div className="mb-4 flex items-center gap-3 p-3 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)]">
          <button
            onClick={toggleSelectAll}
            className="flex items-center gap-1.5 text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-accent)]"
          >
            {notes.length > 0 && selectedIds.size === notes.length
              ? <Square size={15} />
              : <CheckSquare size={15} />}
            全选
          </button>
          <span className="text-xs text-[var(--color-text-tertiary)]">已选 {selectedIds.size} 条</span>
          <div className="flex-1" />
          <button
            onClick={() => setBatchConfirm('restore')}
            disabled={selectedIds.size === 0 || busy}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[var(--radius-md)] text-sm text-[var(--color-accent)] border border-[var(--color-accent)] hover:bg-[var(--color-accent-bg)] disabled:opacity-40 transition-colors"
          >
            <RotateCcw size={14} />
            批量恢复
          </button>
          <button
            onClick={() => setBatchConfirm('permanent')}
            disabled={selectedIds.size === 0 || busy}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[var(--radius-md)] text-sm text-white bg-red-500 hover:bg-red-600 disabled:opacity-40 transition-colors"
          >
            <Trash2 size={14} />
            批量彻底删除
          </button>
        </div>
      )}

      {/* 列表 */}
      {loading ? (
        <div className="text-center py-16 text-sm text-[var(--color-text-tertiary)]">加载中...</div>
      ) : notes.length === 0 ? (
        <div className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)]">
          <EmptyState
            icon={<Inbox size={40} />}
            title="回收站是空的"
            description="删除的笔记会在这里保留 14 天，期间可以随时恢复。"
          />
        </div>
      ) : (
        <div className="space-y-3">
          {notes.map((note) => (
            <div
              key={note.id}
              className="flex items-start gap-3 p-4 rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)] hover:border-[var(--color-accent)] transition-colors"
            >
              {/* 勾选（批量管理模式） */}
              {selectMode && (
                <button
                  onClick={() => toggleSelect(note.id)}
                  className="mt-0.5 text-[var(--color-text-tertiary)] hover:text-[var(--color-accent)]"
                >
                  {selectedIds.has(note.id) ? <CheckSquare size={18} /> : <Square size={18} />}
                </button>
              )}

              {/* 笔记信息 */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-[var(--color-text)] truncate">
                    {note.title}
                  </span>
                  <span className={`text-xs px-2 py-0.5 rounded-full ${daysBadgeClass(note.days_remaining)}`}>
                    {daysLabel(note)}
                  </span>
                </div>
                {note.content && (
                  <p className="mt-1 text-xs text-[var(--color-text-tertiary)] line-clamp-1 truncate">
                    {note.content.replace(/[#>*`\-\s]+/g, ' ').slice(0, 120)}
                  </p>
                )}
                <p className="mt-1.5 text-xs text-[var(--color-text-tertiary)]">
                  删除时间：
                  {note.deleted_at ? new Date(note.deleted_at).toLocaleString('zh-CN') : '-'}
                  {note.category ? ` · 分类：${note.category}` : ''}
                </p>
              </div>

              {/* 操作按钮 */}
              <div className="flex items-center gap-1 shrink-0">
                {!selectMode && (
                  <>
                    <button
                      onClick={() => setSingleConfirm({ type: 'restore', note })}
                      disabled={busy}
                      className="p-2 rounded-[var(--radius-md)] text-[var(--color-accent)] hover:bg-[var(--color-accent-bg)] disabled:opacity-40 transition-colors"
                      title="恢复笔记"
                    >
                      <RotateCcw size={16} />
                    </button>
                    <button
                      onClick={() => setSingleConfirm({ type: 'permanent', note })}
                      disabled={busy}
                      className="p-2 rounded-[var(--radius-md)] text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-40 transition-colors"
                      title="彻底删除（不可恢复）"
                    >
                      <Trash2 size={16} />
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}

          {/* 分页 */}
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-4 pt-2">
              <button
                onClick={() => fetchList(page - 1)}
                disabled={page <= 1 || busy}
                className="flex items-center gap-1 px-3 py-1.5 rounded-[var(--radius-md)] border border-[var(--color-border)] text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-accent)] disabled:opacity-40"
              >
                <ChevronLeft size={14} /> 上一页
              </button>
              <span className="text-xs text-[var(--color-text-tertiary)]">
                第 {page} / {totalPages} 页 · 共 {total} 条
              </span>
              <button
                onClick={() => fetchList(page + 1)}
                disabled={page >= totalPages || busy}
                className="flex items-center gap-1 px-3 py-1.5 rounded-[var(--radius-md)] border border-[var(--color-border)] text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-accent)] disabled:opacity-40"
              >
                下一页 <ChevronRight size={14} />
              </button>
            </div>
          )}
        </div>
      )}

      {/* 单条确认弹窗 */}
      <ConfirmDialog
        open={singleConfirm !== null}
        title={singleConfirm?.type === 'restore' ? '恢复笔记' : '彻底删除笔记'}
        description={
          singleConfirm?.type === 'restore'
            ? `确定恢复「${singleConfirm?.note.title}」？恢复后将重新出现在笔记列表中。`
            : `彻底删除「${singleConfirm?.note.title}」后无法恢复，其回顾记录也会一并删除。确定？`
        }
        confirmLabel={singleConfirm?.type === 'restore' ? '恢复' : '彻底删除'}
        variant={singleConfirm?.type === 'permanent' ? 'danger' : 'default'}
        onConfirm={() => {
          if (!singleConfirm) return;
          if (singleConfirm.type === 'restore') handleRestore(singleConfirm.note);
          else handlePermanentDelete(singleConfirm.note);
        }}
        onCancel={() => setSingleConfirm(null)}
      />

      {/* 批量确认弹窗 */}
      <ConfirmDialog
        open={batchConfirm !== null}
        title={batchConfirm === 'restore' ? '批量恢复' : '批量彻底删除'}
        description={
          batchConfirm === 'restore'
            ? `确定恢复选中的 ${selectedIds.size} 条笔记？`
            : `彻底删除选中的 ${selectedIds.size} 条笔记后无法恢复，其回顾记录也会一并删除。确定？`
        }
        confirmLabel={batchConfirm === 'restore' ? '恢复' : '彻底删除'}
        variant={batchConfirm === 'permanent' ? 'danger' : 'default'}
        onConfirm={() => {
          if (batchConfirm) handleBatch(batchConfirm);
        }}
        onCancel={() => setBatchConfirm(null)}
      />
        </>
      ) : (
        /* ===== 分类回收站（分类管理功能新增，设计 §4.7） ===== */
        <>
          {/* 分类 tab 头：批量管理开关（与笔记 tab 独立） */}
          {categories.length > 0 && !catsLoading && (
            <div className="mb-4 flex items-center justify-between">
              <span className="text-xs text-[var(--color-text-tertiary)]">
                共 {categories.length} 个分类
              </span>
              <button
                onClick={() => {
                  setCatSelectMode(!catSelectMode);
                  setCatSelectedIds(new Set());
                }}
                className="flex items-center gap-2 px-4 py-2 rounded-[var(--radius-md)] border border-[var(--color-border)] text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-accent)] hover:border-[var(--color-accent)] transition-colors"
              >
                {catSelectMode ? <RotateCcw size={15} /> : <CheckSquare size={15} />}
                {catSelectMode ? '退出批量管理' : '批量管理'}
              </button>
            </div>
          )}

          {/* 分类批量操作栏 */}
          {catSelectMode && (
            <div className="mb-4 flex items-center gap-3 p-3 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-bg)]">
              <button
                onClick={toggleCatSelectAll}
                className="flex items-center gap-1.5 text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-accent)]"
              >
                {categories.length > 0 && catSelectedIds.size === categories.length
                  ? <Square size={15} />
                  : <CheckSquare size={15} />}
                全选
              </button>
              <span className="text-xs text-[var(--color-text-tertiary)]">已选 {catSelectedIds.size} 个</span>
              <div className="flex-1" />
              <button
                onClick={() => setCatBatchConfirm('restore')}
                disabled={catSelectedIds.size === 0 || busy}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-[var(--radius-md)] text-sm text-[var(--color-accent)] border border-[var(--color-accent)] hover:bg-[var(--color-accent-bg)] disabled:opacity-40 transition-colors"
              >
                <RotateCcw size={14} />
                批量恢复
              </button>
              <button
                onClick={() => setCatBatchConfirm('permanent')}
                disabled={catSelectedIds.size === 0 || busy}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-[var(--radius-md)] text-sm text-white bg-red-500 hover:bg-red-600 disabled:opacity-40 transition-colors"
              >
                <Trash2 size={14} />
                批量彻底删除
              </button>
            </div>
          )}

          {catsLoading ? (
            <div className="text-center py-16 text-sm text-[var(--color-text-tertiary)]">加载中...</div>
          ) : categories.length === 0 ? (
            <div className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)]">
              <EmptyState
                icon={<Inbox size={40} />}
                title="回收站是空的"
                description="删除的分类会在这里保留 14 天，期间可以随时恢复。"
              />
            </div>
          ) : (
            <div className="space-y-3">
              {categories.map((cat) => (
                <div
                  key={cat.id}
                  className="flex items-center gap-3 p-4 rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)] hover:border-[var(--color-accent)] transition-colors"
                >
                  {/* 勾选（批量管理模式） */}
                  {catSelectMode && (
                    <button
                      onClick={() => toggleCatSelect(cat.id)}
                      className="mt-0.5 text-[var(--color-text-tertiary)] hover:text-[var(--color-accent)] shrink-0"
                    >
                      {catSelectedIds.has(cat.id) ? <CheckSquare size={18} /> : <Square size={18} />}
                    </button>
                  )}

                  {/* 分类信息 */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-[var(--color-text)] truncate">
                        {cat.icon ? cat.icon : <Folder size={14} className="inline text-[var(--color-accent)]" />}
                        {' '}{cat.name}
                        {cat.descendant_count > 0 && (
                          <span className="ml-1 text-xs text-[var(--color-text-tertiary)]">
                            （{cat.descendant_count} 个子分类）
                          </span>
                        )}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${daysBadgeClass(cat.days_remaining)}`}>
                        {cat.days_remaining <= 0 ? '即将自动清理' : `剩余 ${cat.days_remaining} 天`}
                      </span>
                    </div>
                    <p className="mt-1.5 text-xs text-[var(--color-text-tertiary)]">
                      删除时间：
                      {cat.deleted_at ? new Date(cat.deleted_at).toLocaleString('zh-CN') : '-'}
                    </p>
                  </div>

                  {/* 操作按钮 */}
                  {!catSelectMode && (
                    <div className="flex items-center gap-1 shrink-0">
                      <button
                        onClick={() => setCatSingleConfirm({ type: 'restore', cat })}
                        disabled={busy}
                        className="p-2 rounded-[var(--radius-md)] text-[var(--color-accent)] hover:bg-[var(--color-accent-bg)] disabled:opacity-40 transition-colors"
                        title="恢复分类"
                      >
                        <RotateCcw size={16} />
                      </button>
                      <button
                        onClick={() => setCatSingleConfirm({ type: 'permanent', cat })}
                        disabled={busy}
                        className="p-2 rounded-[var(--radius-md)] text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-40 transition-colors"
                        title="彻底删除（不可恢复）"
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* 分类单条确认弹窗 */}
          <ConfirmDialog
            open={catSingleConfirm !== null}
            title={catSingleConfirm?.type === 'restore' ? '恢复分类' : '彻底删除分类'}
            description={
              catSingleConfirm?.type === 'restore'
                ? catSingleConfirm.cat.descendant_count > 0
                  ? `确定恢复分类「${catSingleConfirm.cat.name}」？其 ${catSingleConfirm.cat.descendant_count} 个子分类将一并恢复。`
                  : `确定恢复分类「${catSingleConfirm.cat.name}」？恢复后将重新出现在分类树中。`
                : `彻底删除分类「${catSingleConfirm?.cat.name}」后无法恢复，其子分类将一并删除。确定？`
            }
            confirmLabel={catSingleConfirm?.type === 'restore' ? '恢复' : '彻底删除'}
            variant={catSingleConfirm?.type === 'permanent' ? 'danger' : 'default'}
            onConfirm={() => {
              if (!catSingleConfirm) return;
              if (catSingleConfirm.type === 'restore') handleRestoreCategory(catSingleConfirm.cat);
              else handlePermanentDeleteCategory(catSingleConfirm.cat);
            }}
            onCancel={() => setCatSingleConfirm(null)}
          />

          {/* 分类批量确认弹窗 */}
          <ConfirmDialog
            open={catBatchConfirm !== null}
            title={catBatchConfirm === 'restore' ? '批量恢复分类' : '批量彻底删除分类'}
            description={
              catBatchConfirm === 'restore'
                ? `确定恢复选中的 ${catSelectedIds.size} 个分类？其子分类将一并恢复。`
                : `彻底删除选中的 ${catSelectedIds.size} 个分类后无法恢复，其子分类将一并删除。确定？`
            }
            confirmLabel={catBatchConfirm === 'restore' ? '恢复' : '彻底删除'}
            variant={catBatchConfirm === 'permanent' ? 'danger' : 'default'}
            onConfirm={() => {
              if (catBatchConfirm) handleCatBatch(catBatchConfirm);
            }}
            onCancel={() => setCatBatchConfirm(null)}
          />
        </>
      )}
    </div>
  );
}
