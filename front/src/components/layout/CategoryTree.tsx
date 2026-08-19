/**
 * 分类管理面板（v2 笔记页三栏第一栏，融合 test.md）
 *
 * 静态树形分类（前端配置 constants/noteCategoryTree.ts，不动后端）：
 * - 与后端 category 对齐的节点走精确过滤，数量徽标取 list({page_size:1}).total（与回收站角标同法）
 * - 静态占位节点（脚本/项目文档/闪念/阅读/三体/Flutter…）数量固定 0，点击后列表显示空态（决策 D3）
 * - 新建分类 / 新建子分类按钮本期为 UI 占位（后端无分类 CRUD，决策 M4）
 * - 视图切换按钮（四宫格）= 树全部展开/折叠（决策 M1）
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { ChevronLeft, ChevronRight, Folder, FolderPlus, LayoutGrid, Plus } from 'lucide-react';
import { notesApi } from '../../api/notes';
import {
  CATEGORY_TREE,
  DEFAULT_EXPANDED_KEYS,
  type CategoryTreeNode,
} from '../../constants/noteCategoryTree';

interface CategoryTreeProps {
  selectedKey: string;
  onSelect: (node: CategoryTreeNode) => void;
  /** 折叠本栏（由 NoteList 控制栏位宽度切换） */
  onToggleCollapse?: () => void;
}

/** 递归渲染树节点 */
function TreeNodeRow({
  node,
  depth,
  selectedKey,
  expandedKeys,
  counts,
  onToggleExpand,
  onSelect,
  newSubCategoryLabel,
  comingSoon,
}: {
  node: CategoryTreeNode;
  depth: number;
  selectedKey: string;
  expandedKeys: Set<string>;
  counts: Record<string, number>;
  onToggleExpand: (key: string) => void;
  onSelect: (node: CategoryTreeNode) => void;
  newSubCategoryLabel: string;
  comingSoon: () => void;
}) {
  const hasChildren = !!node.children?.length;
  const expanded = expandedKeys.has(node.key);
  const isSelected = selectedKey === node.key;

  return (
    <div>
      {/* 分类项行：折叠箭头 + 文件夹图标 + 名称 + 数量徽标 + 新建子分类 */}
      <div
        className={`group flex items-center gap-1.5 py-1.5 pr-2 rounded-lg cursor-pointer transition-colors duration-150 ${
          isSelected ? 'bg-[var(--color-tree-selected)]' : 'hover:bg-[var(--color-surface-alt)]'
        }`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => onSelect(node)}
      >
        {/* 折叠箭头（仅存在子分类时显示） */}
        {hasChildren ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggleExpand(node.key);
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

        <Folder size={15} className="text-[var(--color-accent)] shrink-0" />

        <span
          className={`flex-1 truncate text-sm ${
            isSelected ? 'text-[var(--color-accent)] font-medium' : 'text-[var(--color-text)]'
          }`}
        >
          {node.label}
        </span>

        {/* 笔记数量标识（灰色小方块） */}
        <span className="min-w-5 h-5 px-1 rounded text-[10px] leading-5 text-center bg-[var(--color-count-badge-bg)] text-[var(--color-count-badge-text)] shrink-0">
          {counts[node.key] ?? 0}
        </span>

        {/* 新建子分类（hover 显示，占位） */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            comingSoon();
          }}
          className="p-0.5 rounded text-[var(--color-text-tertiary)]
            opacity-0 group-hover:opacity-100 hover:text-[var(--color-accent)]
            transition-opacity duration-150 shrink-0"
          title={newSubCategoryLabel}
        >
          <FolderPlus size={13} />
        </button>
      </div>

      {/* 子分类（展开时渲染，带淡入过渡） */}
      {hasChildren && expanded && (
        <div className="animate-fade-in">
          {node.children!.map((child) => (
            <TreeNodeRow
              key={child.key}
              node={child}
              depth={depth + 1}
              selectedKey={selectedKey}
              expandedKeys={expandedKeys}
              counts={counts}
              onToggleExpand={onToggleExpand}
              onSelect={onSelect}
              newSubCategoryLabel={newSubCategoryLabel}
              comingSoon={comingSoon}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function CategoryTree({ selectedKey, onSelect, onToggleCollapse }: CategoryTreeProps) {
  const { t } = useTranslation();
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set(DEFAULT_EXPANDED_KEYS));
  const [counts, setCounts] = useState<Record<string, number>>({});

  /** 挂载时并行拉取对齐节点的数量徽标（page_size:1 仅取 total，成本低） */
  useEffect(() => {
    const next: Record<string, number> = {};
    const tasks: Promise<void>[] = [];

    const visit = (nodes: CategoryTreeNode[]) => {
      for (const n of nodes) {
        if (n.key === 'all') {
          tasks.push(
            notesApi
              .list({ page: 1, page_size: 1 })
              .then((res) => {
                next[n.key] = res.data.data?.total ?? 0;
              })
              .catch(() => {
                // 数量加载失败不影响树渲染（显示 0）
              })
          );
        } else if (n.category !== undefined) {
          tasks.push(
            notesApi
              .list({ page: 1, page_size: 1, category: n.category })
              .then((res) => {
                next[n.key] = res.data.data?.total ?? 0;
              })
              .catch(() => {
                // 同上
              })
          );
        } else {
          // 静态占位节点固定 0（决策 D3）
          next[n.key] = 0;
        }
        if (n.children) visit(n.children);
      }
    };
    visit(CATEGORY_TREE);

    Promise.all(tasks).then(() => setCounts(next));
  }, []);

  /** 展开/折叠单个节点 */
  const toggleExpand = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  /** 视图切换按钮：全部展开/折叠（决策 M1） */
  const toggleAll = () => {
    setExpandedKeys((prev) => {
      const allKeys: string[] = [];
      const collect = (nodes: CategoryTreeNode[]) => {
        for (const n of nodes) {
          if (n.children) {
            allKeys.push(n.key);
            collect(n.children);
          }
        }
      };
      collect(CATEGORY_TREE);
      // 已全部展开 → 全部折叠；否则全部展开
      const allExpanded = allKeys.every((k) => prev.has(k));
      return allExpanded ? new Set() : new Set(allKeys);
    });
  };

  /** 新建分类占位提示（决策 M4） */
  const comingSoon = () => toast.info(t('note.categoryComingSoon'));

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
            onClick={comingSoon}
            className="p-1.5 rounded-md bg-[var(--color-quick-action-bg)] text-[var(--color-quick-action-text)]
              hover:opacity-80 transition-opacity duration-150"
            title={t('note.newCategory')}
          >
            <Plus size={15} />
          </button>
        </div>
      </div>

      {/* 树形分类目录（独立滚动） */}
      <div className="flex-1 overflow-y-auto p-2">
        {CATEGORY_TREE.map((node) => (
          <TreeNodeRow
            key={node.key}
            node={node}
            depth={0}
            selectedKey={selectedKey}
            expandedKeys={expandedKeys}
            counts={counts}
            onToggleExpand={toggleExpand}
            onSelect={onSelect}
            newSubCategoryLabel={t('note.newSubCategory')}
            comingSoon={comingSoon}
          />
        ))}
      </div>
    </div>
  );
}
