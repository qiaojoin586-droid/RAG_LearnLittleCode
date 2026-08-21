/**
 * 笔记列表页（v2 三栏式笔记管理界面，融合 test.md）
 *
 * 三栏布局（置于 main 内容区，各栏独立滚动，栏间 1px 细边框）：
 * - 第一栏 CategoryTree（220px）：动态分类树（后端 /category/tree）+ category_id 过滤 + 数量徽标
 * - 第二栏 笔记列表（320px）：竖排卡片 NoteCardItem，保留分页/批量/删除，新增收藏星/排序/视图切换
 * - 第三栏 NotePreviewPanel（自适应）：预览区（本期空状态，决策 D4）
 *
 * v2 移除：页面搜索框（由 TopBar 全局搜索承接，阶段四）、分类筛选选项卡（由分类树承接）。
 * 新增：排序（客户端）/ 视图切换（卡片 ↔ 网格，决策 M1）/ MD-TXT 徽章 / 收藏星切换（is_pinned）。
 */

import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate, useParams, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { CheckSquare, LayoutGrid, Trash2, X, FileText, PanelLeft, ChevronLeft, ChevronRight, Folder } from 'lucide-react';
import { notesApi } from '../api/notes';
import { useNoteContextStore } from '../stores/useNoteContextStore';
import EmptyState from '../components/common/EmptyState';
import LoadingSkeleton from '../components/common/LoadingSkeleton';
import ConfirmDialog from '../components/common/ConfirmDialog';
import CategoryTree from '../components/layout/CategoryTree';
import NoteCardItem from '../components/layout/NoteCardItem';
import NotePreviewPanel from '../components/layout/NotePreviewPanel';
import NoteMoveDialog from '../components/note/NoteMoveDialog';
import NoteEditor from './NoteEditor';
import NoteFormatPicker from '../components/layout/NoteFormatPicker';
import { useCategoryStore, UNCATEGORIZED_SENTINEL } from '../stores/useCategoryStore';
import { findCategory } from '../types/category';
import type { Note } from '../types/api';

type SortOrder = 'updated' | 'created' | 'title';
type ViewMode = 'card' | 'grid';

/** 折叠态窄条（v2 交互优化）：展开按钮 + 图标 + 竖向栏目标识 */
function CollapsedStrip({
  icon: Icon,
  label,
  onExpand,
}: {
  icon: React.ElementType;
  label: string;
  onExpand: () => void;
}) {
  return (
    <div className="h-full flex flex-col items-center pt-3 pb-4">
      <button
        onClick={onExpand}
        className="p-1.5 rounded-lg text-[var(--color-text-tertiary)]
          hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]
          transition-colors duration-150"
        title={label}
      >
        <ChevronRight size={16} />
      </button>
      <div className="flex-1 flex flex-col items-center justify-center gap-2 min-h-0">
        <Icon size={16} className="text-[var(--color-text-tertiary)]" />
        <span className="text-xs text-[var(--color-text-secondary)] whitespace-nowrap [writing-mode:vertical-rl] select-none">
          {label}
        </span>
      </div>
    </div>
  );
}

export default function NoteList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  // 新建模式：/notes/new（新建按钮进入）
  const isNewMode = location.pathname === '/notes/new';

  const [notes, setNotes] = useState<Note[]>([]);
  const [loading, setLoading] = useState(true);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);

  // 分类树选中态（Zustand：null = 所有笔记；__uncategorized__ = 未分类；其余为分类 ID）
  const selectedCategoryId = useCategoryStore((s) => s.selectedCategoryId);
  const categories = useCategoryStore((s) => s.categories);

  /** 当前选中分类的显示名（列表栏标题用） */
  const selectedLabel = useMemo(() => {
    if (selectedCategoryId === null) return t('note.allNotes');
    if (selectedCategoryId === UNCATEGORIZED_SENTINEL) return t('note.uncategorized');
    return findCategory(categories, selectedCategoryId)?.name ?? t('note.allNotes');
  }, [selectedCategoryId, categories, t]);

  // 排序 / 视图切换（决策 M1）/ 批量 / 删除
  const [sortOrder, setSortOrder] = useState<SortOrder>('updated');
  const [viewMode, setViewMode] = useState<ViewMode>('card');
  const [batchMode, setBatchMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deleteTarget, setDeleteTarget] = useState<string | 'batch' | null>(null);
  // 批量移动笔记到分类（分类管理功能新增）
  const [moveDialogOpen, setMoveDialogOpen] = useState(false);

  // 移动端：分类树抽屉显隐（<md 时树以 fixed 抽屉展示，见响应式策略 §8.2）
  const [treeDrawerOpen, setTreeDrawerOpen] = useState(false);

  // 两栏折叠状态（v2 交互优化：独立折叠、详情区自动加宽；localStorage 持久化）
  const [treeCollapsed, setTreeCollapsed] = useState(
    () => localStorage.getItem('notes-tree-collapsed') === '1'
  );
  const [listCollapsed, setListCollapsed] = useState(
    () => localStorage.getItem('notes-list-collapsed') === '1'
  );

  // 折叠状态持久化：刷新后保持上次布局
  useEffect(() => {
    localStorage.setItem('notes-tree-collapsed', treeCollapsed ? '1' : '0');
    localStorage.setItem('notes-list-collapsed', listCollapsed ? '1' : '0');
  }, [treeCollapsed, listCollapsed]);

  // 右侧详情区选中的笔记（null = 空状态或新建模式；点击卡片 → navigate(/notes/:id) 同步）
  const [selectedNote, setSelectedNote] = useState<Note | null>(null);

  // 路由（/notes/:id、/notes/new）→ 同步选中态：列表已有数据直接用；深链接则拉详情
  // 同时写入「当前浏览笔记」上下文（AI 浮层自动关联用，实时跟随切换）
  useEffect(() => {
    if (isNewMode || !id) {
      setSelectedNote(null);
      useNoteContextStore.getState().setCurrentNote(null);
      return;
    }
    const existing = notes.find((n) => n.id === id);
    if (existing) {
      setSelectedNote(existing);
      useNoteContextStore.getState().setCurrentNote(existing);
      return;
    }
    let cancelled = false;
    notesApi
      .detail(id)
      .then((res) => {
        if (!cancelled) {
          setSelectedNote(res.data.data);
          useNoteContextStore.getState().setCurrentNote(res.data.data);
        }
      })
      .catch(() => {
        if (!cancelled) {
          toast.error('加载笔记失败');
          navigate('/notes', { replace: true });
        }
      });
    return () => {
      cancelled = true;
    };
    // notes 不参与依赖（保存/加载后通过 setSelectedNote 直接同步，避免循环）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, isNewMode]);

  // 离开笔记页（卸载）时清除上下文，避免其他页面浮层仍关联旧笔记
  useEffect(
    () => () => {
      useNoteContextStore.getState().setCurrentNote(null);
    },
    []
  );

  /** 编辑器保存回调：更新成功后同步列表卡片、选中态与笔记上下文（浮层自动关联取最新内容） */
  const handleNoteUpdated = useCallback((updated: Note) => {
    setNotes((prev) => prev.map((n) => (n.id === updated.id ? { ...n, ...updated } : n)));
    setSelectedNote(updated);
    useNoteContextStore.getState().setCurrentNote(updated);
  }, []);

  /** 编辑器保存回调：新建成功后插入列表顶部并切换 URL 到新笔记 */
  const handleNoteCreated = useCallback(
    (created: Note) => {
      setNotes((prev) => [created, ...prev]);
      setTotal((t) => t + 1);
      setSelectedNote(created);
      navigate(`/notes/${created.id}`, { replace: true });
    },
    [navigate]
  );

  /** 加载笔记列表（reset=true 从第 1 页开始；按分类 ID 过滤，null = 不过滤） */
  const loadNotes = useCallback(
    async (reset = false, categoryId?: string | null) => {
      const target = categoryId === undefined ? selectedCategoryId : categoryId;
      setLoading(true);
      try {
        const p = reset ? 1 : page;
        const res = await notesApi.list({
          page: p,
          page_size: 20,
          category_id: target ?? undefined,
        });
        const data = res.data.data;
        setNotes(reset ? data.notes : [...notes, ...data.notes]);
        setTotal(data.total);
        if (!reset) setPage(p + 1);
      } catch {
        // 错误处理（与旧版一致：静默失败，列表保持原状）
      } finally {
        setLoading(false);
      }
    },
    [selectedCategoryId, page, notes]
  );

  // 分类树选择变化时重新加载（重置到第 1 页）。
  // 注意：loadNotes 依赖 notes/page，加入依赖数组会导致"加载 → notes 变化 →
  // loadNotes 身份变化 → effect 重跑"的循环，故仅以 selectedCategoryId 为依赖
  useEffect(() => {
    loadNotes(true, selectedCategoryId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCategoryId]);

  // 移动端：选中分类后自动收起分类树抽屉
  useEffect(() => {
    setTreeDrawerOpen(false);
  }, [selectedCategoryId]);

  /** 客户端排序（仅作用于已加载列表；分页数据追加后同样参与排序） */
  const sortedNotes = useMemo(() => {
    const arr = [...notes];
    if (sortOrder === 'title') {
      arr.sort((a, b) => a.title.localeCompare(b.title, 'zh'));
    } else if (sortOrder === 'created') {
      arr.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
    } else {
      arr.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
    }
    return arr;
  }, [notes, sortOrder]);

  /** 收藏/取消收藏（is_pinned 切换，现有字段 + notesApi.update） */
  const handlePin = useCallback(
    async (note: Note) => {
      try {
        await notesApi.update(note.id, { is_pinned: !note.is_pinned });
        setNotes((prev) =>
          prev.map((n) => (n.id === note.id ? { ...n, is_pinned: !note.is_pinned } : n))
        );
      } catch {
        toast.error(t('common.error'));
      }
    },
    [t]
  );

  /** 单条删除笔记（软删除 → 回收站）；若删除的是正在编辑的笔记则清空详情区 */
  const handleDelete = useCallback(
    async (noteId: string) => {
      try {
        await notesApi.delete(noteId);
        setNotes((prev) => prev.filter((n) => n.id !== noteId));
        setTotal((prev) => prev - 1);
        toast.success('删除成功');
      } catch {
        toast.error('删除失败');
      }
      setDeleteTarget(null);
      if (selectedNote?.id === noteId) {
        setSelectedNote(null);
        navigate('/notes', { replace: true });
      }
    },
    [selectedNote, navigate]
  );

  /** 批量删除笔记；若包含正在编辑的笔记则清空详情区 */
  const handleBatchDelete = useCallback(async () => {
    if (selectedIds.size === 0) return;
    try {
      await notesApi.batch({ note_ids: Array.from(selectedIds), operation: 'delete' });
      setNotes((prev) => prev.filter((n) => !selectedIds.has(n.id)));
      setTotal((prev) => prev - selectedIds.size);
      toast.success(`已删除 ${selectedIds.size} 条笔记`);
      setSelectedIds(new Set());
      setBatchMode(false);
    } catch {
      toast.error('批量删除失败');
    }
    setDeleteTarget(null);
    if (selectedNote && selectedIds.has(selectedNote.id)) {
      setSelectedNote(null);
      navigate('/notes', { replace: true });
    }
  }, [selectedIds, selectedNote, navigate]);

  /** 切换笔记选中状态 */
  const toggleSelect = useCallback((noteId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(noteId)) next.delete(noteId);
      else next.add(noteId);
      return next;
    });
  }, []);

  /** 打开笔记编辑器（稳定引用，配合 NoteCardItem memo） */
  const handleOpenNote = useCallback(
    (note: Note) => navigate(`/notes/${note.id}`),
    [navigate]
  );

  /** 请求删除笔记（稳定引用，配合 NoteCardItem memo） */
  const handleRequestDelete = useCallback((note: Note) => {
    setDeleteTarget(note.id);
  }, []);

  /** 退出批量模式 */
  const exitBatchMode = () => {
    setBatchMode(false);
    setSelectedIds(new Set());
  };

  /** 详情区是否激活（选中笔记或新建模式） */
  const detailActive = isNewMode || selectedNote !== null;

  /** 新建模式文档类型（/notes/new?format=txt|md，默认 md） */
  const newFormat: 'md' | 'txt' =
    new URLSearchParams(location.search).get('format') === 'txt' ? 'txt' : 'md';

  /** 渲染一张笔记卡片（回调均为稳定引用，配合 NoteCardItem 的 React.memo） */
  const renderNoteCard = (note: Note) => (
    <NoteCardItem
      key={note.id}
      note={note}
      batchMode={batchMode}
      isSelected={selectedIds.has(note.id)}
      onPin={handlePin}
      onDelete={handleRequestDelete}
      onToggleSelect={toggleSelect}
      onOpen={handleOpenNote}
    />
  );

  return (
    <div className="flex h-full">
      {/* ===== 第一栏：分类管理面板（移动端为抽屉，桌面端内联；可折叠为窄条） ===== */}
      <div
        className={`shrink-0 border-r border-[var(--color-panel-border)] bg-[var(--color-sidebar-bg)] md:static transition-[width] duration-300 ease-in-out overflow-hidden ${
          treeCollapsed ? 'md:w-10' : 'md:w-[220px]'
        } ${treeDrawerOpen ? 'fixed left-14 top-14 bottom-0 z-50 w-[220px]' : 'hidden md:block'}`}
      >
        {/* 折叠时保持挂载（hidden 仅隐藏），展开后树状态（展开节点/数量徽标）不丢失 */}
        <div className={treeCollapsed ? 'hidden' : 'block h-full'}>
          <CategoryTree onToggleCollapse={() => setTreeCollapsed(true)} />
        </div>
        {/* 折叠窄条（仅桌面端展示） */}
        {treeCollapsed && (
          <div className="hidden md:block h-full">
            <CollapsedStrip
              icon={Folder}
              label={t('note.categoryManager')}
              onExpand={() => setTreeCollapsed(false)}
            />
          </div>
        )}
      </div>

      {/* 移动端分类树遮罩 */}
      {treeDrawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/20 md:hidden"
          onClick={() => setTreeDrawerOpen(false)}
        />
      )}

      {/* ===== 第二栏：笔记列表面板（可折叠为窄条；移动端占满，详情激活时移动端隐藏） ===== */}
      <div className={`${detailActive ? 'hidden md:flex' : 'flex'} flex-1 md:flex-none min-w-0 border-r border-[var(--color-panel-border)] flex-col transition-[width] duration-300 ease-in-out overflow-hidden ${
        listCollapsed ? 'md:w-10' : 'md:w-[320px]'
      }`}>
        {/* 内容区（折叠时桌面端隐藏但保持挂载，展开后列表状态不丢失） */}
        <div className={`${listCollapsed ? 'md:hidden' : ''} h-full flex flex-col`}>
        {/* 顶部操作栏 */}
        <div className="flex items-center justify-between px-3 h-14 border-b border-[var(--color-panel-border)] shrink-0">
          <div className="flex items-center gap-1.5 min-w-0">
            {/* 移动端：呼出分类树抽屉（先取消折叠，避免抽屉为空） */}
            <button
              onClick={() => {
                setTreeCollapsed(false);
                setTreeDrawerOpen(true);
              }}
              className="p-1.5 rounded-md text-[var(--color-text-secondary)]
                hover:bg-[var(--color-surface-alt)] md:hidden shrink-0"
              title={t('note.categoryManager')}
            >
              <PanelLeft size={15} />
            </button>
            <h1 className="text-base font-bold text-[var(--color-text)] truncate">
              {selectedLabel}
            </h1>
          </div>

          {batchMode ? (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-[var(--color-text-secondary)]">
                {t('note.selectedCount', { count: selectedIds.size })}
              </span>
              <button
                onClick={() => selectedIds.size > 0 && setMoveDialogOpen(true)}
                disabled={selectedIds.size === 0}
                className="p-1.5 rounded-md text-[var(--color-text-secondary)]
                  hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-accent)]
                  disabled:opacity-50 transition-colors"
                title={t('note.moveToCategory')}
              >
                <Folder size={15} />
              </button>
              <button
                onClick={() => selectedIds.size > 0 && setDeleteTarget('batch')}
                disabled={selectedIds.size === 0}
                className="p-1.5 rounded-md bg-[var(--color-danger)] text-white
                  hover:opacity-90 disabled:opacity-50 transition-opacity"
                title={t('common.delete')}
              >
                <Trash2 size={15} />
              </button>
              <button
                onClick={exitBatchMode}
                className="p-1.5 rounded-md text-[var(--color-text-secondary)]
                  hover:bg-[var(--color-surface-alt)] transition-colors"
                title={t('common.cancel')}
              >
                <X size={15} />
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1">
              {/* 排序筛选（客户端排序） */}
              <select
                value={sortOrder}
                onChange={(e) => setSortOrder(e.target.value as SortOrder)}
                className="h-8 px-1.5 rounded-md text-xs
                  bg-[var(--color-card)] border border-[var(--color-border)]
                  text-[var(--color-text-secondary)] focus:outline-none cursor-pointer"
                title={t('note.sort')}
              >
                <option value="updated">{t('note.sortUpdated')}</option>
                <option value="created">{t('note.sortCreated')}</option>
                <option value="title">{t('note.sortTitle')}</option>
              </select>

              {/* 视图切换（卡片 ↔ 网格，决策 M1） */}
              <button
                onClick={() => setViewMode((v) => (v === 'card' ? 'grid' : 'card'))}
                className={`p-1.5 rounded-md transition-colors ${
                  viewMode === 'grid'
                    ? 'text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-alt)]'
                }`}
                title={t('note.viewToggle')}
              >
                <LayoutGrid size={15} />
              </button>

              {/* 批量模式入口（决策 M2 保留） */}
              <button
                onClick={() => setBatchMode(true)}
                className="p-1.5 rounded-md text-[var(--color-text-secondary)]
                  hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-accent)]
                  transition-colors"
                title={t('note.batch')}
              >
                <CheckSquare size={15} />
              </button>

              {/* 新建笔记（文档类型功能：点击弹出类型选择，默认 Markdown） */}
              <NoteFormatPicker />

              {/* 折叠本栏（v2 交互优化：收起为窄条，详情区自动加宽） */}
              <button
                onClick={() => setListCollapsed(true)}
                className="p-1.5 rounded-md text-[var(--color-text-tertiary)]
                  hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-text)]
                  transition-colors"
                title={t('note.collapse')}
              >
                <ChevronLeft size={15} />
              </button>
            </div>
          )}
        </div>

        {/* 笔记列表（独立滚动） */}
        <div className="flex-1 overflow-y-auto">
          {loading && notes.length === 0 ? (
            <LoadingSkeleton fullPage />
          ) : notes.length === 0 ? (
            <EmptyState
              icon={<FileText size={48} />}
              title={t('note.empty')}
              description={t('note.emptyDesc')}
              action={
                <button
                  onClick={() => navigate('/notes/new')}
                  className="px-4 py-2 rounded-[var(--radius-md)] bg-[var(--color-accent)] text-white text-sm"
                >
                  {t('common.create')}
                </button>
              }
            />
          ) : viewMode === 'card' ? (
            /* 竖排卡片（test.md 核心形态） */
            <div className="flex flex-col gap-3 p-3">
              {sortedNotes.map(renderNoteCard)}
              {notes.length < total && (
                <div className="text-center py-2">
                  <button
                    onClick={() => loadNotes()}
                    disabled={loading}
                    className="px-6 py-2 text-sm text-[var(--color-accent)] border border-[var(--color-border)]
                      rounded-xl hover:bg-[var(--color-accent-bg)] transition-all duration-200"
                  >
                    {loading ? t('common.loading') : t('note.loadMore')}
                  </button>
                </div>
              )}
            </div>
          ) : (
            /* 网格视图（320px 栏内为单列大间距卡片，与卡片视图区分密度） */
            <div className="grid grid-cols-1 gap-4 p-4">
              {sortedNotes.map(renderNoteCard)}
              {notes.length < total && (
                <div className="text-center py-2">
                  <button
                    onClick={() => loadNotes()}
                    disabled={loading}
                    className="px-6 py-2 text-sm text-[var(--color-accent)] border border-[var(--color-border)]
                      rounded-xl hover:bg-[var(--color-accent-bg)] transition-all duration-200"
                  >
                    {loading ? t('common.loading') : t('note.loadMore')}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
        </div>

        {/* 折叠窄条（仅桌面端展示） */}
        {listCollapsed && (
          <div className="hidden md:block h-full">
            <CollapsedStrip
              icon={FileText}
              label={t('nav.notes')}
              onExpand={() => setListCollapsed(false)}
            />
          </div>
        )}
      </div>

      {/* ===== 第三栏：详情区（空状态 ↔ 内联笔记编辑器，无页面跳转） ===== */}
      <div className={`${detailActive ? 'flex' : 'hidden'} md:flex flex-1 min-w-0 flex-col`}>
        {detailActive ? (
          <NoteEditor
            key={`${selectedNote?.id ?? 'new'}-${newFormat}`}
            note={selectedNote}
            initialFormat={newFormat}
            onCreated={handleNoteCreated}
            onUpdated={handleNoteUpdated}
            onBackMobile={() => navigate('/notes')}
          />
        ) : (
          <NotePreviewPanel />
        )}
      </div>

      {/* 删除确认弹窗（单条；ConfirmDialog 在无 transform 的 main 内正常工作） */}
      <ConfirmDialog
        open={deleteTarget !== null && deleteTarget !== 'batch'}
        title="删除笔记"
        description="确定要删除这条笔记吗？删除后可在回收站恢复。"
        confirmLabel="删除"
        variant="danger"
        onConfirm={() => deleteTarget && handleDelete(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
      />

      {/* 批量删除确认弹窗 */}
      <ConfirmDialog
        open={deleteTarget === 'batch'}
        title="批量删除"
        description={`确定要删除选中的 ${selectedIds.size} 条笔记吗？删除后可在回收站恢复。`}
        confirmLabel={`删除 ${selectedIds.size} 条`}
        variant="danger"
        onConfirm={handleBatchDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      {/* 批量移动笔记到分类（分类管理功能新增） */}
      <NoteMoveDialog
        open={moveDialogOpen}
        noteIds={Array.from(selectedIds)}
        onClose={() => setMoveDialogOpen(false)}
        onMoved={() => {
          setMoveDialogOpen(false);
          exitBatchMode();
          // 重新加载当前分类列表（移动后可能不在当前过滤范围内）
          setNotes([]);
          setPage(1);
          loadNotes(true);
        }}
      />
    </div>
  );
}
