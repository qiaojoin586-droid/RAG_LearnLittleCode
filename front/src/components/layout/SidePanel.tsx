/**
 * 弹出式面板（v2 新壳层，阶段四：各模式列表）
 *
 * /notes 路由下由 MainLayout 强制折叠（笔记浏览由笔记页三栏承担）。
 * 其余模式数据源均为现有前端 API，不动后端：
 * - review:      reviewApi.getToday()         今日回顾（标题 + 下次复习时间）
 * - knowledge:   knowledgeApi.listDocuments() 知识库文档（文件名 + 分块数）
 * - ppt:         pptTemplatesApi.list()       PPT 模板（名称 + 文件大小）
 * - recycle-bin: notesApi.listDeleted()       回收站（标题 + 剩余天数）
 *
 * 点击条目跳转对应页面（页面本身是完整功能入口）。
 */

import { memo, useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { BookOpen, ChevronLeft, FileText, Library, Presentation, Trash2 } from 'lucide-react';
import { reviewApi } from '../../api/review';
import { knowledgeApi } from '../../api/knowledge';
import { pptTemplatesApi, type PptTemplateInfo } from '../../api/pptTemplates';
import { notesApi } from '../../api/notes';
import type { PanelType } from './IconRail';
import type { ReviewRecord, KnowledgeDocument, DeletedNote } from '../../types/api';

interface SidePanelProps {
  activePanel: PanelType | null;
  onClose: () => void;
}

const PANEL_TITLES: Record<PanelType, string> = {
  notes: 'nav.notes',
  review: 'nav.review',
  knowledge: 'nav.knowledge',
  ppt: 'nav.ppt_templates',
  'recycle-bin': 'nav.recycle_bin',
};

/** 面板列表项描述（各模式统一渲染） */
interface PanelListItem {
  key: string;
  title: string;
  meta?: string;
  icon: React.ElementType;
  onClick: () => void;
}

/** 紧凑列表行（memo：列表刷新时跳过未变化的行） */
const PanelRow = memo(function PanelRow({ item }: { item: PanelListItem }) {
  const Icon = item.icon;
  return (
    <button
      onClick={item.onClick}
      className="w-full flex items-center gap-2.5 px-4 py-3 border-b border-[var(--color-panel-border)]
        text-left hover:bg-[var(--color-surface-alt)] transition-colors duration-150"
    >
      <Icon size={15} className="text-[var(--color-text-tertiary)] shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-sm text-[var(--color-text)] truncate">{item.title}</p>
        {item.meta && <p className="text-[10px] text-[var(--color-text-tertiary)]">{item.meta}</p>}
      </div>
    </button>
  );
});

export default function SidePanel({ activePanel, onClose }: SidePanelProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [listItems, setListItems] = useState<PanelListItem[]>([]);
  const [loading, setLoading] = useState(false);

  /** 按面板类型拉取列表数据（切换面板时重新加载） */
  const fetchPanelData = useCallback(
    async (panel: PanelType) => {
      setLoading(true);
      try {
        if (panel === 'review') {
          const res = await reviewApi.getToday();
          const reviews: ReviewRecord[] = res.data?.data?.reviews ?? [];
          setListItems(
            reviews.map((r) => ({
              key: `review-${r.review_id}`,
              title: r.note_title,
              meta: `${t('panel.nextReview')} ${new Date(r.next_review_at).toLocaleDateString()} · ${r.review_count} ${t('panel.times')}`,
              icon: BookOpen,
              onClick: () => navigate('/review'),
            }))
          );
        } else if (panel === 'knowledge') {
          const res = await knowledgeApi.listDocuments();
          const docs: KnowledgeDocument[] = res.data?.data?.documents ?? [];
          setListItems(
            docs.map((d) => ({
              key: `doc-${d.id}`,
              title: d.filename,
              meta: `${d.chunk_count} ${t('panel.chunks')}`,
              icon: Library,
              onClick: () => navigate('/knowledge'),
            }))
          );
        } else if (panel === 'ppt') {
          const res = await pptTemplatesApi.list();
          const templates: PptTemplateInfo[] = res.data?.data?.templates ?? [];
          setListItems(
            templates.map((tmpl) => ({
              key: `ppt-${tmpl.id}`,
              title: tmpl.name,
              meta: `${(tmpl.file_size / 1024).toFixed(1)} KB`,
              icon: Presentation,
              onClick: () => navigate('/ppt-templates'),
            }))
          );
        } else if (panel === 'recycle-bin') {
          const res = await notesApi.listDeleted({ page: 1, page_size: 50 });
          const deleted: DeletedNote[] = res.data?.data?.notes ?? [];
          setListItems(
            deleted.map((n) => ({
              key: `del-${n.id}`,
              title: n.title,
              meta: t('panel.recycleDays', { days: n.days_remaining }),
              icon: Trash2,
              onClick: () => navigate('/recycle-bin'),
            }))
          );
        }
      } catch {
        // 加载失败静默处理（面板非核心路径，空列表展示）
      } finally {
        setLoading(false);
      }
    },
    [navigate, t]
  );

  // 面板类型变化时拉取对应列表
  useEffect(() => {
    if (activePanel && activePanel !== 'notes') {
      fetchPanelData(activePanel);
    } else {
      setListItems([]);
    }
  }, [activePanel, fetchPanelData]);

  return (
    <div className="flex flex-col h-full w-full">
      {/* 面板头部：标题 + 折叠按钮 */}
      <div className="flex items-center justify-between px-4 h-14 border-b border-[var(--color-panel-border)] shrink-0">
        <h2 className="text-base font-semibold text-[var(--color-text)]">
          {activePanel ? t(PANEL_TITLES[activePanel]) : ''}
        </h2>
        <button
          onClick={onClose}
          className="p-1.5 rounded-lg text-[var(--color-text-tertiary)]
            hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-text-secondary)]
            transition-colors duration-150"
          title={t('panel.close')}
        >
          <ChevronLeft size={18} />
        </button>
      </div>

      {/* 面板内容：对应模式的列表 */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <p className="text-center text-xs text-[var(--color-text-tertiary)] py-10">
            {t('common.loading')}
          </p>
        ) : listItems.length === 0 ? (
          <div className="flex flex-col items-center py-14 text-[var(--color-text-tertiary)]">
            <FileText size={32} className="mb-3 opacity-50" />
            <p className="text-xs">{t('panel.empty')}</p>
          </div>
        ) : (
          listItems.map((item) => <PanelRow key={item.key} item={item} />)
        )}
      </div>
    </div>
  );
}
