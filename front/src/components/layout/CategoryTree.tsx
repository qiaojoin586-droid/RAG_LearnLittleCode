/**
 * 分类管理面板（v2 笔记页三栏第一栏；分类管理功能改造版）
 *
 * 数据源：useCategoryStore（GET /category/tree 后端动态分类树，替代原静态 noteCategoryTree.ts）
 * 功能：
 * - 树形展示（展开/折叠、全部展开/折叠、栏位折叠）
 * - 数量徽标：后端 note_count（含子孙）；「所有笔记」= 顶层合计 + 未分类
 * - 选中：store.selectedCategoryId（null = 所有笔记；__uncategorized__ = 未分类）
 * - 新建/新建子分类/编辑/删除（影响范围确认）/合并到…（右键菜单 + 顶栏按钮）
 * - 拖拽：同级排序（SortableContext）+ 拖到其他分类下移动（row droppable）
 *   · 移动的循环/层级/同名校验由后端兜底，失败 toast 后端消息并刷新树
 *
 * 注意：本组件渲染于 NoteList 的 main 区域（无 transform 祖先），fixed 弹窗定位正常。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import {
  ChevronLeft, ChevronRight, Folder, FolderPlus, LayoutGrid, Plus,
  Pencil, Trash2, GitMerge, GripVertical,
} from 'lucide-react';
import {
  DndContext, DragOverlay, PointerSensor, useSensor, useSensors,
  useDroppable, closestCenter,
  type UniqueIdentifier, type DragStartEvent, type DragOverEvent, type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext, useSortable, verticalListSortingStrategy, arrayMove,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useCategoryStore, extractError, UNCATEGORIZED_SENTINEL } from '../../stores/useCategoryStore';
import type { Category } from '../../types/category';
import CategoryDialog from '../note/CategoryDialog';
import CategoryDeleteDialog from '../note/CategoryDeleteDialog';
import CategoryMergeDialog from '../note/CategoryMergeDialog';

interface CategoryTreeProps {
  /** 折叠本栏（由 NoteList 控制栏位宽度切换） */
  onToggleCollapse?: () => void;
}

/** 右键菜单坐标与目标 */
interface MenuState {
  x: number;
  y: number;
  node: Category;
}

/** 拖拽起止标识（onto:{id} = 拖到该分类下作为子分类） */
const ONTO_PREFIX = 'onto:';
const isOntoId = (id: UniqueIdentifier | undefined) =>
  typeof id === 'string' && id.startsWith(ONTO_PREFIX);

/** 单个分类行（可拖拽排序 + 可接受"拖入成为子分类"） */
function SortableRow({
  node,
  depth,
  selected,
  expanded,
  ontoHighlight,
  onToggleExpand,
  onSelect,
  onContextMenu,
  onNewSub,
}: {
  node: Category;
  depth: number;
  selected: boolean;
  expanded: boolean;
  ontoHighlight: boolean;
  onToggleExpand: () => void;
  onSelect: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
  onNewSub: () => void;
}) {
  const hasChildren = !!node.children?.length;

  // 同一元素同时作为 sortable（同级排序）与 droppable（拖入成为子分类）
  const {
    attributes, listeners, setNodeRef: setSortableRef,
    transform, transition, isDragging,
  } = useSortable({ id: node.id });
  const { setNodeRef: setDroppableRef, isOver } = useDroppable({ id: `${ONTO_PREFIX}${node.id}` });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={(el) => {
        setSortableRef(el);
        setDroppableRef(el);
      }}
      style={style}
      {...attributes}
      {...listeners}
      onClick={onSelect}
      onContextMenu={onContextMenu}
      className={`group flex items-center gap-1.5 py-1.5 pr-2 rounded-lg cursor-pointer transition-colors duration-150 select-none ${
        isDragging
          ? 'opacity-40'
          : selected
            ? 'bg-[var(--color-tree-selected)]'
            : 'hover:bg-[var(--color-surface-alt)]'
      } ${ontoHighlight ? 'ring-2 ring-[var(--color-accent)]' : ''} ${isOver && !ontoHighlight ? 'bg-[var(--color-accent-bg)]' : ''}`}
    >
      <div style={{ paddingLeft: 8 + depth * 14 }} className="flex items-center gap-1.5 flex-1 min-w-0">
        {/* 拖拽把手（hover 显示） */}
        <GripVertical size={12} className="opacity-0 group-hover:opacity-100 text-[var(--color-text-tertiary)] shrink-0 -ml-1" />

        {/* 折叠箭头（仅存在子分类时显示） */}
        {hasChildren ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggleExpand();
            }}
            className="p-0.5 rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)] shrink-0"
          >
            <ChevronRight
              size={14}
              className={`transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}
            />
          </button>
        ) : (
          <span className="w-[18px] shrink-0" />
        )}

        {/* 图标（自定义 emoji 优先，否则文件夹；颜色作为左侧色点） */}
        {node.icon ? (
          <span className="text-sm leading-none shrink-0">{node.icon}</span>
        ) : (
          <Folder
            size={15}
            className="shrink-0"
            style={node.color ? { color: node.color } : { color: 'var(--color-accent)' }}
          />
        )}
        {node.color && node.icon && (
          <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: node.color }} />
        )}

        <span
          className={`flex-1 truncate text-sm ${
            selected ? 'text-[var(--color-accent)] font-medium' : 'text-[var(--color-text)]'
          }`}
        >
          {node.name}
        </span>

        {/* 笔记数量标识（灰色小方块） */}
        <span className="min-w-5 h-5 px-1 rounded text-[10px] leading-5 text-center bg-[var(--color-count-badge-bg)] text-[var(--color-count-badge-text)] shrink-0">
          {node.note_count}
        </span>

        {/* 新建子分类（hover 显示） */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onNewSub();
          }}
          className="p-0.5 rounded text-[var(--color-text-tertiary)]
            opacity-0 group-hover:opacity-100 hover:text-[var(--color-accent)]
            transition-opacity duration-150 shrink-0"
        >
          <FolderPlus size={13} />
        </button>
      </div>
    </div>
  );
}

/** 递归渲染子树 */
function TreeNode({
  node,
  depth,
  selectedId,
  expandedKeys,
  ontoId,
  onToggleExpand,
  onSelect,
  onContextMenu,
  onNewSub,
}: {
  node: Category;
  depth: number;
  selectedId: string | null;
  expandedKeys: Set<string>;
  ontoId: string | null;
  onToggleExpand: (key: string) => void;
  onSelect: (id: string) => void;
  onContextMenu: (e: React.MouseEvent, node: Category) => void;
  onNewSub: (node: Category) => void;
}) {
  const hasChildren = !!node.children?.length;
  const expanded = expandedKeys.has(node.id);

  return (
    <div>
      <SortableRow
        node={node}
        depth={depth}
        selected={selectedId === node.id}
        expanded={expanded}
        ontoHighlight={ontoId === node.id}
        onToggleExpand={() => onToggleExpand(node.id)}
        onSelect={() => onSelect(node.id)}
        onContextMenu={(e) => onContextMenu(e, node)}
        onNewSub={() => onNewSub(node)}
      />

      {/* 子分类（展开时渲染，同级为 SortableContext 支持排序） */}
      {hasChildren && expanded && (
        <div className="animate-fade-in">
          <SortableContext
            items={node.children!.map((c) => c.id)}
            strategy={verticalListSortingStrategy}
          >
            {node.children!.map((child) => (
              <TreeNode
                key={child.id}
                node={child}
                depth={depth + 1}
                selectedId={selectedId}
                expandedKeys={expandedKeys}
                ontoId={ontoId}
                onToggleExpand={onToggleExpand}
                onSelect={onSelect}
                onContextMenu={onContextMenu}
                onNewSub={onNewSub}
              />
            ))}
          </SortableContext>
        </div>
      )}
    </div>
  );
}

/** 拖拽预览浮层 */
function DragPreview({ node }: { node: Category }) {
  return (
    <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[var(--color-card)] border border-[var(--color-border)] shadow-[var(--shadow-dialog)]">
      {node.icon ? <span className="text-sm">{node.icon}</span> : <Folder size={15} className="text-[var(--color-accent)]" />}
      <span className="text-sm text-[var(--color-text)]">{node.name}</span>
    </div>
  );
}

export default function CategoryTree({ onToggleCollapse }: CategoryTreeProps) {
  const { t } = useTranslation();
  const {
    categories, uncategorizedCount, selectedCategoryId,
    fetchCategories, selectCategory, moveCategory, reorderCategories,
  } = useCategoryStore();

  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [dialogState, setDialogState] = useState<
    { mode: 'create'; parentId?: string } | { mode: 'edit'; category: Category } | null
  >(null);
  const [deleteTarget, setDeleteTarget] = useState<Category | null>(null);
  const [mergeSource, setMergeSource] = useState<Category | null>(null);

  // 拖拽状态
  const [activeNode, setActiveNode] = useState<Category | null>(null);
  const [ontoId, setOntoId] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } })
  );

  // 挂载时拉取分类树
  useEffect(() => {
    fetchCategories();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 首次加载后默认展开全部节点（树最多 3 级、体量小，展开便于浏览与拖拽排序）
  useEffect(() => {
    if (categories.length === 0) return;
    setExpandedKeys((prev) => {
      if (prev.size > 0) return prev;
      const next = new Set<string>();
      const collect = (nodes: Category[]) => {
        for (const n of nodes) {
          if (n.children?.length) {
            next.add(n.id);
            collect(n.children);
          }
        }
      };
      collect(categories);
      return next;
    });
  }, [categories]);

  // 右键菜单：点击其他区域关闭
  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => {
      if (menuRef.current?.contains(e.target as Node)) return;
      setMenu(null);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [menu]);

  /** id → 分类 扁平映射（拖拽校验用） */
  const flatMap = useMemo(() => {
    const map = new Map<string, Category>();
    const visit = (nodes: Category[]) => {
      for (const n of nodes) {
        map.set(n.id, n);
        if (n.children?.length) visit(n.children);
      }
    };
    visit(categories);
    return map;
  }, [categories]);

  /** id → 父级 key（'root' 表示顶级） */
  const parentKeyOf = useMemo(() => {
    const map = new Map<string, string>();
    const visit = (nodes: Category[], parentKey: string) => {
      for (const n of nodes) {
        map.set(n.id, parentKey);
        if (n.children?.length) visit(n.children, n.id);
      }
    };
    visit(categories, 'root');
    return map;
  }, [categories]);

  /** 父级 key → 子分类 ID 列表（拖拽排序用） */
  const siblingGroups = useMemo(() => {
    const map = new Map<string, string[]>();
    const visit = (nodes: Category[], parentKey: string) => {
      map.set(parentKey, nodes.map((n) => n.id));
      for (const n of nodes) {
        if (n.children?.length) visit(n.children, n.id);
      }
    };
    visit(categories, 'root');
    return map;
  }, [categories]);

  /** 节点深度（最多 3 级，逐级向上查） */
  const depthOf = useMemo(() => {
    const map = new Map<string, number>();
    const visit = (nodes: Category[], depth: number) => {
      for (const n of nodes) {
        map.set(n.id, depth);
        if (n.children?.length) visit(n.children, depth + 1);
      }
    };
    visit(categories, 0);
    return map;
  }, [categories]);

  /** 「所有笔记」总数 = 顶层 note_count 合计 + 未分类 */
  const allNotesCount = useMemo(
    () => categories.reduce((sum, c) => sum + (c.note_count ?? 0), 0) + uncategorizedCount,
    [categories, uncategorizedCount]
  );

  /** 展开/折叠单个节点 */
  const toggleExpand = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  /** 视图切换按钮：全部展开/折叠（决策 M1 保留） */
  const toggleAll = () => {
    setExpandedKeys((prev) => {
      const allKeys: string[] = [];
      const collect = (nodes: Category[]) => {
        for (const n of nodes) {
          if (n.children?.length) {
            allKeys.push(n.id);
            collect(n.children);
          }
        }
      };
      collect(categories);
      const allExpanded = allKeys.length > 0 && allKeys.every((k) => prev.has(k));
      return allExpanded ? new Set() : new Set(allKeys);
    });
  };

  /** 拖拽结束：onto 命中 = 移动为子分类；同组命中 = 同级排序 */
  const handleDragEnd = async (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveNode(null);
    setOntoId(null);
    if (!over) return;

    const activeId = String(active.id);

    if (isOntoId(over.id)) {
      // 拖入某分类下 → 移动
      const targetId = String(over.id).slice(ONTO_PREFIX.length);
      if (targetId === activeId || parentKeyOf.get(activeId) === targetId) return;
      try {
        await moveCategory(activeId, targetId);
        toast.success(t('note.categoryMoved'));
      } catch (err) {
        toast.error(extractError(err) || t('common.error'));
      }
      return;
    }

    // 同级排序：active 与 over 在同一父级组内
    const group = siblingGroups.get(parentKeyOf.get(activeId) ?? 'root');
    const overId = String(over.id);
    if (group && group.includes(overId) && activeId !== overId) {
      const oldIndex = group.indexOf(activeId);
      const newIndex = group.indexOf(overId);
      const ordered = arrayMove(group, oldIndex, newIndex);
      const parentKey = parentKeyOf.get(activeId) ?? 'root';
      try {
        await reorderCategories({
          parent_id: parentKey === 'root' ? null : parentKey,
          ordered_ids: ordered,
        });
        toast.success(t('note.reorderSaved'));
      } catch (err) {
        toast.error(extractError(err) || t('common.error'));
      }
    }
  };

  const handleDragOver = (event: DragOverEvent) => {
    setOntoId(isOntoId(event.over?.id) ? String(event.over!.id).slice(ONTO_PREFIX.length) : null);
  };

  const handleDragStart = (event: DragStartEvent) => {
    setActiveNode(flatMap.get(String(event.active.id)) ?? null);
  };

  /** 打开右键菜单（限真实分类节点） */
  const openMenu = (e: React.MouseEvent, node: Category) => {
    e.preventDefault();
    e.stopPropagation();
    const x = Math.min(e.clientX, window.innerWidth - 180);
    const y = Math.min(e.clientY, window.innerHeight - 180);
    setMenu({ x, y, node });
  };

  /** 普通行（所有笔记 / 未分类，无拖拽与右键菜单） */
  const renderPseudoRow = (
    key: string,
    icon: React.ReactNode,
    label: string,
    count: number,
    selectValue: string | null
  ) => (
    <div
      key={key}
      onClick={() => selectCategory(selectValue)}
      className={`group flex items-center gap-1.5 py-1.5 pr-2 pl-2 rounded-lg cursor-pointer transition-colors duration-150 ${
        selectedCategoryId === selectValue
          ? 'bg-[var(--color-tree-selected)]'
          : 'hover:bg-[var(--color-surface-alt)]'
      }`}
    >
      {icon}
      <span
        className={`flex-1 truncate text-sm ${
          selectedCategoryId === selectValue
            ? 'text-[var(--color-accent)] font-medium'
            : 'text-[var(--color-text)]'
        }`}
      >
        {label}
      </span>
      <span className="min-w-5 h-5 px-1 rounded text-[10px] leading-5 text-center bg-[var(--color-count-badge-bg)] text-[var(--color-count-badge-text)] shrink-0">
        {count}
      </span>
    </div>
  );

  return (
    <div className="flex flex-col h-full">
      {/* 顶部操作区：标题 + 折叠 + 视图切换 + 新建分类 */}
      <div className="flex items-center justify-between px-3 h-14 border-b border-[var(--color-panel-border)] shrink-0">
        <h2 className="text-sm font-bold text-[var(--color-text)]">{t('note.categoryManager')}</h2>
        <div className="flex items-center gap-1">
          {/* 折叠本栏（v2 交互优化：收起为窄条，详情区自动加宽） */}
          {onToggleCollapse && (
            <button
              onClick={onToggleCollapse}
              className="p-1.5 rounded-md text-[var(--color-text-tertiary)]
                hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-text)]
                transition-colors duration-150"
              title={t('note.collapse')}
            >
              <ChevronLeft size={15} />
            </button>
          )}
          <button
            onClick={toggleAll}
            className="p-1.5 rounded-md bg-[var(--color-quick-action-bg)] text-[var(--color-quick-action-text)]
              hover:opacity-80 transition-opacity duration-150"
            title={t('note.expandCollapseAll')}
          >
            <LayoutGrid size={15} />
          </button>
          <button
            onClick={() => setDialogState({ mode: 'create' })}
            className="p-1.5 rounded-md bg-[var(--color-quick-action-bg)] text-[var(--color-quick-action-text)]
              hover:opacity-80 transition-opacity duration-150"
            title={t('note.newCategory')}
          >
            <Plus size={15} />
          </button>
        </div>
      </div>

      {/* 树形分类目录（独立滚动；DndContext 包裹以支持拖拽） */}
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragOver={handleDragOver}
        onDragEnd={handleDragEnd}
        onDragCancel={() => {
          setActiveNode(null);
          setOntoId(null);
        }}
      >
        <div className="flex-1 overflow-y-auto p-2">
          {/* 伪节点：所有笔记 / 未分类 */}
          {renderPseudoRow(
            'all',
            <LayoutGrid size={15} className="text-[var(--color-accent)] shrink-0" />,
            t('note.allNotes'),
            allNotesCount,
            null
          )}
          {renderPseudoRow(
            UNCATEGORIZED_SENTINEL,
            <Folder size={15} className="text-[var(--color-text-tertiary)] shrink-0" />,
            t('note.uncategorized'),
            uncategorizedCount,
            UNCATEGORIZED_SENTINEL
          )}

          {/* 真实分类树（顶级为 SortableContext 支持排序） */}
          <SortableContext
            items={categories.map((c) => c.id)}
            strategy={verticalListSortingStrategy}
          >
            {categories.map((node) => (
              <TreeNode
                key={node.id}
                node={node}
                depth={0}
                selectedId={selectedCategoryId}
                expandedKeys={expandedKeys}
                ontoId={ontoId}
                onToggleExpand={toggleExpand}
                onSelect={(id) => selectCategory(id)}
                onContextMenu={openMenu}
                onNewSub={(n) => setDialogState({ mode: 'create', parentId: n.id })}
              />
            ))}
          </SortableContext>
        </div>

        {/* 拖拽预览浮层 */}
        <DragOverlay>{activeNode ? <DragPreview node={activeNode} /> : null}</DragOverlay>
      </DndContext>

      {/* 右键菜单 */}
      {menu && (
        <div
          ref={menuRef}
          className="fixed z-[2147483646] w-40 py-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] shadow-[var(--shadow-dialog)]"
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {(depthOf.get(menu.node.id) ?? 0) < 2 && (
            <button
              onClick={() => {
                setDialogState({ mode: 'create', parentId: menu.node.id });
                setMenu(null);
              }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-alt)] transition-colors"
            >
              <FolderPlus size={14} className="text-[var(--color-text-secondary)]" />
              {t('note.newSubCategory')}
            </button>
          )}
          <button
            onClick={() => {
              setDialogState({ mode: 'edit', category: menu.node });
              setMenu(null);
            }}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-alt)] transition-colors"
          >
            <Pencil size={14} className="text-[var(--color-text-secondary)]" />
            {t('note.editCategory')}
          </button>
          <button
            onClick={() => {
              setMergeSource(menu.node);
              setMenu(null);
            }}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-alt)] transition-colors"
          >
            <GitMerge size={14} className="text-[var(--color-text-secondary)]" />
            {t('note.mergeCategory')}
          </button>
          <div className="my-1 h-px bg-[var(--color-border)]" />
          <button
            onClick={() => {
              setDeleteTarget(menu.node);
              setMenu(null);
            }}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
          >
            <Trash2 size={14} />
            {t('note.deleteCategory')}
          </button>
        </div>
      )}

      {/* 创建 / 编辑对话框 */}
      <CategoryDialog
        open={dialogState !== null}
        category={dialogState?.mode === 'edit' ? dialogState.category : undefined}
        parentId={dialogState?.mode === 'create' ? dialogState.parentId : undefined}
        onClose={() => setDialogState(null)}
      />

      {/* 删除确认弹窗 */}
      {deleteTarget && (
        <CategoryDeleteDialog
          open
          category={deleteTarget}
          onClose={() => setDeleteTarget(null)}
        />
      )}

      {/* 合并对话框 */}
      {mergeSource && (
        <CategoryMergeDialog
          open
          source={mergeSource}
          onClose={() => setMergeSource(null)}
        />
      )}
    </div>
  );
}
