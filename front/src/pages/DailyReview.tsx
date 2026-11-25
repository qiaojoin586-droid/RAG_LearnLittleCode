/**
 * 每日回顾页面
 *
 * 功能：
 * 1. 展示今日待回顾的笔记列表（基于艾宾浩斯遗忘曲线）
 * 2. 逐条回顾，标记掌握程度（1-5 分）
 * 3. 回顾统计面板（连续天数、完成率等）
 *
 * v2 布局优化：
 * - 左侧：今日回顾笔记列表（点击条目 → 右下预览区展示只读完整内容）
 * - 右上：回顾答题区（进度条 + 卡片 + 操作栏，垂直空间压缩）
 * - 右下：笔记内容预览区（只读 Markdown 渲染，占位提示「点击左侧笔记查看完整内容」）
 */

import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { BookOpen, CheckCircle, BarChart3, Sparkles } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github.css';
import { reviewApi } from '../api/review';
import { notesApi } from '../api/notes';
import EmptyState from '../components/common/EmptyState';
import { useReviewBridgeStore } from '../stores/useReviewBridgeStore';
import { isHtml, htmlToMarkdown, normalizeNoteContent, escapeInlineHtml } from '../utils/markdownCompat';
import type { ReviewRecord, ReviewStats, ReviewQuestion } from '../types/api';

/** 掌握程度选项（1-5） */
const qualityOptions = [
  { value: 1, label: '完全不记得', color: 'bg-red-100 text-red-700 border-red-200' },
  { value: 2, label: '有点印象', color: 'bg-orange-100 text-orange-700 border-orange-200' },
  { value: 3, label: '勉强记住', color: 'bg-yellow-100 text-yellow-700 border-yellow-200' },
  { value: 4, label: '比较熟悉', color: 'bg-blue-100 text-blue-700 border-blue-200' },
  { value: 5, label: '完全掌握', color: 'bg-green-100 text-green-700 border-green-200' },
];

export default function DailyReview() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [reviews, setReviews] = useState<ReviewRecord[]>([]);
  const [stats, setStats] = useState<ReviewStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [showStats, setShowStats] = useState(false);
  const [completing, setCompleting] = useState(false);

  // AI 复习题（按需生成，6h 缓存）
  const [questions, setQuestions] = useState<ReviewQuestion[]>([]);
  const [aiLoading, setAiLoading] = useState(false);
  const [revealed, setRevealed] = useState<Record<number, boolean>>({});

  // 预览区状态（侧边栏选择条目后加载完整笔记内容，只读展示）
  const [previewTitle, setPreviewTitle] = useState('');
  const [previewContent, setPreviewContent] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  // 竞态保护：快速连续点击时仅最新一次请求生效
  const previewReqRef = useRef(0);

  // 侧边栏（SidePanel 每日回顾列表）选择桥接：点击条目 → 答题卡片与预览区同步切换
  const selectedReviewId = useReviewBridgeStore((s) => s.selectedReviewId);
  const selectSeq = useReviewBridgeStore((s) => s.selectSeq);

  /** 加载今日回顾列表和统计 */
  useEffect(() => {
    (async () => {
      try {
        const [reviewsRes, statsRes] = await Promise.all([
          reviewApi.getToday(),
          reviewApi.getStats(),
        ]);
        setReviews(reviewsRes.data.data.reviews);
        setStats(statsRes.data.data);
      } catch {
        toast.error('加载回顾数据失败');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  /** 选中条目：拉取完整笔记内容用于右下预览（只读） */
  const handleSelectReview = async (review: ReviewRecord) => {
    const reqId = ++previewReqRef.current;
    setPreviewLoading(true);
    try {
      const res = await notesApi.detail(review.note_id);
      if (reqId !== previewReqRef.current) return;
      const note = res.data.data;
      const raw = note.content || '';
      setPreviewTitle(note.title);
      // 兼容旧数据：HTML 转 Markdown → 归一化（剥外层围栏/CRLF）→ 转义行内角括号
      //（ReactMarkdown 会把 <T> 等泛型符号当原始 HTML 丢弃，需转义保留）
      setPreviewContent(escapeInlineHtml(normalizeNoteContent(isHtml(raw) ? htmlToMarkdown(raw) : raw)));
    } catch {
      if (reqId !== previewReqRef.current) return;
      // 详情失败：用列表自带的内容兜底（可能不含完整正文）
      if (review.note_content) {
        setPreviewTitle(review.note_title);
        setPreviewContent(
          escapeInlineHtml(
            normalizeNoteContent(
              isHtml(review.note_content) ? htmlToMarkdown(review.note_content) : review.note_content
            )
          )
        );
      } else {
        setPreviewTitle('');
        setPreviewContent('');
        toast.error('加载笔记内容失败');
      }
    } finally {
      if (reqId === previewReqRef.current) setPreviewLoading(false);
    }
  };

  // 侧边栏选择同步：切换答题卡片并加载预览（reviews 异步加载完成后命中即同步）
  useEffect(() => {
    if (selectedReviewId === null || reviews.length === 0) return;
    const idx = reviews.findIndex((r) => r.review_id === selectedReviewId);
    if (idx < 0) return;
    setCurrentIndex(idx);
    handleSelectReview(reviews[idx]);
    // selectSeq 驱动（同条目重复点击也会触发）；reviews 参与依赖以便加载完成后同步
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectSeq, reviews]);

  /** 标记当前笔记回顾完成 */
  const handleComplete = async (quality: number) => {
    const review = reviews[currentIndex];
    if (!review) return;

    setCompleting(true);
    try {
      await reviewApi.complete(review.review_id, quality);
      // 从列表中移除已回顾项
      setReviews((prev) => prev.filter((r) => r.review_id !== review.review_id));
      // 更新统计
      setStats((prev) =>
        prev ? { ...prev, completed_today: prev.completed_today + 1, pending_today: Math.max(0, prev.pending_today - 1) } : prev
      );
      // 通知侧边栏列表刷新（SidePanel 订阅 listVersion 重新拉取）
      useReviewBridgeStore.getState().bumpList();
      toast.success('回顾完成');
    } catch {
      toast.error(t('common.error'));
    } finally {
      setCompleting(false);
    }
  };

  /** 当前正在回顾的笔记 */
  const currentReview = reviews[currentIndex];

  // 切换回顾条目时清空上一条的 AI 复习题
  useEffect(() => {
    setQuestions([]);
    setRevealed({});
  }, [currentReview?.review_id]);

  /** AI 生成复习题（同一笔记内容 6 小时内命中缓存，不重复计费） */
  const handleGenerateQuestions = async () => {
    const review = reviews[currentIndex];
    if (!review || aiLoading) return;
    setAiLoading(true);
    setRevealed({});
    try {
      const res = await reviewApi.generateQuestions(review.review_id);
      const data = res.data.data;
      if (data.questions?.length) {
        setQuestions(data.questions);
      } else {
        toast.error(t('review.ai.failed'));
      }
    } catch {
      toast.error(t('review.ai.failed'));
    } finally {
      setAiLoading(false);
    }
  };

  const isFinished = reviews.length === 0 && !loading;
  const progress = stats ? (stats.completed_today / (stats.completed_today + stats.pending_today)) * 100 : 0;

  return (
    <div className="p-6 h-full flex flex-col overflow-hidden">
      {/* 页面头部（压缩间距） */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <h1 className="text-2xl font-heading font-bold text-[var(--color-text)]">
          {t('nav.review')}
        </h1>
        <button
          onClick={() => setShowStats(!showStats)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-[var(--radius-md)] border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)]"
        >
          <BarChart3 size={16} />
          {t('review.stats')}
        </button>
      </div>

      {/* 统计面板（压缩间距） */}
      {showStats && stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4 shrink-0">
          <StatCard label="今日已完成" value={stats.completed_today} />
          <StatCard label="今日待回顾" value={stats.pending_today} />
          <StatCard label="累计回顾" value={stats.total_reviews} />
          <StatCard label="连续天数" value={`${stats.streak_days} 天`} />
        </div>
      )}

      {/* 主体：答题区（压缩）+ 预览区（笔记选择入口为左侧 SidePanel 列表，页面内不再重复） */}
      <div className="flex-1 min-h-0 flex flex-col">
          {/* 上半：答题区（垂直空间压缩） */}
          {stats && stats.pending_today + stats.completed_today > 0 && (
            <div className="mb-3 shrink-0">
              <div className="flex items-center justify-between text-xs text-[var(--color-text-tertiary)] mb-1">
                <span>今日进度</span>
                <span>{stats.completed_today} / {stats.completed_today + stats.pending_today}</span>
              </div>
              <div className="w-full h-2 bg-[var(--color-border)] rounded-full overflow-hidden">
                <div
                  className="h-full bg-[var(--color-accent)] rounded-full transition-all duration-500"
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>
          )}

          {loading ? (
            <p className="text-center text-[var(--color-text-tertiary)]">{t('common.loading')}</p>
          ) : isFinished ? (
            <EmptyState
              icon={<CheckCircle size={48} className="text-green-500" />}
              title="太棒了！今日回顾已全部完成 🎉"
            />
          ) : currentReview ? (
            <div className="shrink-0">
              {/* 回顾卡片（内边距与行距压缩） */}
              <div className="bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)] p-4 shadow-card">
                {/* 标题和元信息 */}
                <div className="flex items-center justify-between mb-2">
                  <h2 className="text-lg font-heading font-bold text-[var(--color-text)] truncate">
                    {currentReview.note_title}
                  </h2>
                  <span className="text-xs text-[var(--color-text-tertiary)] shrink-0 ml-3">
                    第 {currentReview.review_count + 1} 次回顾 · 间隔 {currentReview.interval_days} 天
                  </span>
                </div>

                {/* 操作按钮（保留原功能：跳转笔记详情页） */}
                <div className="flex items-center gap-2 mb-2">
                  <button
                    onClick={() => navigate(`/notes/${currentReview.note_id}`)}
                    className="flex items-center gap-1 text-sm text-[var(--color-accent)] hover:underline"
                  >
                    <BookOpen size={14} />
                    查看原文
                  </button>
                  <button
                    onClick={handleGenerateQuestions}
                    disabled={aiLoading}
                    className="flex items-center gap-1 text-sm text-[var(--color-accent)] hover:underline disabled:opacity-50"
                  >
                    <Sparkles size={14} />
                    {aiLoading ? t('review.ai.loading') : t('review.ai.generate')}
                  </button>
                </div>

                {/* AI 复习题（生成后展示，答案默认折叠） */}
                {questions.length > 0 && (
                  <div className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-alt)] p-3 space-y-2">
                    {questions.map((q, i) => (
                      <div key={i} className="text-sm">
                        <p className="text-[var(--color-text)]">
                          Q{i + 1}. {q.question}
                        </p>
                        <button
                          type="button"
                          onClick={() => setRevealed((prev) => ({ ...prev, [i]: !prev[i] }))}
                          className="mt-0.5 text-xs text-[var(--color-text-tertiary)] hover:underline"
                        >
                          {revealed[i] ? t('review.ai.hideAnswer') : t('review.ai.showAnswer')}
                        </button>
                        {revealed[i] && q.answer && (
                          <p className="mt-1 text-[var(--color-text-secondary)] whitespace-pre-wrap">
                            {q.answer}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* 掌握程度评分 */}
                <p className="text-sm text-[var(--color-text-secondary)] mb-2">
                  你对这篇笔记的掌握程度如何？
                </p>
                <div className="flex flex-wrap gap-2">
                  {qualityOptions.map((opt) => (
                    <button
                      key={opt.value}
                      disabled={completing}
                      onClick={() => handleComplete(opt.value)}
                      className={`
                        px-3 py-1 text-sm rounded-full border transition-all
                        hover:scale-105 disabled:opacity-50
                        ${opt.color}
                      `}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* 跳过 / 上一题（间距压缩） */}
              <div className="flex items-center justify-between mt-2 px-2">
                <button
                  onClick={() => setCurrentIndex((i) => Math.min(i + 1, reviews.length - 1))}
                  disabled={currentIndex >= reviews.length - 1}
                  className="text-sm text-[var(--color-text-tertiary)] hover:text-[var(--color-text)] disabled:opacity-30"
                >
                  跳过
                </button>
                <span className="text-xs text-[var(--color-text-tertiary)]">
                  {currentIndex + 1} / {reviews.length}
                </span>
                <button
                  onClick={() => setCurrentIndex((i) => Math.max(i - 1, 0))}
                  disabled={currentIndex === 0}
                  className="text-sm text-[var(--color-text-tertiary)] hover:text-[var(--color-text)] disabled:opacity-30"
                >
                  上一题
                </button>
              </div>
            </div>
          ) : (
            <EmptyState icon={<BookOpen size={48} />} title={t('review.noReviews')} />
          )}

          {/* 下半：笔记内容预览区（只读） */}
          <div className="flex-1 min-h-0 mt-4 flex flex-col bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)] overflow-hidden">
            <div className="flex items-center justify-between gap-3 px-4 h-12 border-b border-[var(--color-border)] shrink-0">
              <h3 className="text-sm font-semibold text-[var(--color-text)] shrink-0">
                {t('review.previewTitle')}
              </h3>
              {previewTitle && (
                <span className="text-xs text-[var(--color-text-tertiary)] truncate">
                  {previewTitle}
                </span>
              )}
            </div>
            <div className="flex-1 overflow-y-auto p-4 min-h-0">
              {previewLoading ? (
                <p className="text-center text-sm text-[var(--color-text-tertiary)] py-10">
                  {t('common.loading')}
                </p>
              ) : previewContent ? (
                <div className="md-prose">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                    {previewContent}
                  </ReactMarkdown>
                </div>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-center">
                  <BookOpen size={32} className="text-[var(--color-text-tertiary)] opacity-50 mb-3" />
                  <p className="text-sm text-[var(--color-text-tertiary)]">{t('review.clickHint')}</p>
                </div>
              )}
            </div>
          </div>
      </div>
    </div>
  );
}

/** 统计小卡片 */
function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="p-3 bg-[var(--color-card)] rounded-[var(--radius-md)] border border-[var(--color-border)] text-center">
      <div className="text-xl font-bold text-[var(--color-accent)]">{value}</div>
      <div className="text-xs text-[var(--color-text-tertiary)] mt-1">{label}</div>
    </div>
  );
}
