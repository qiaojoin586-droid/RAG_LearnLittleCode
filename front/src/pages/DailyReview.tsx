/**
 * 每日回顾页面
 * 
 * 功能：
 * 1. 展示今日待回顾的笔记列表（基于艾宾浩斯遗忘曲线）
 * 2. 逐条回顾，标记掌握程度（1-5 分）
 * 3. 回顾统计面板（连续天数、完成率等）
 */

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { BookOpen, CheckCircle, BarChart3 } from 'lucide-react';
import { reviewApi } from '../api/review';
import EmptyState from '../components/common/EmptyState';
import type { ReviewRecord, ReviewStats } from '../types/api';

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
      toast.success('回顾完成');
    } catch {
      toast.error(t('common.error'));
    } finally {
      setCompleting(false);
    }
  };

  /** 当前正在回顾的笔记 */
  const currentReview = reviews[currentIndex];
  const isFinished = reviews.length === 0 && !loading;
  const progress = stats ? (stats.completed_today / (stats.completed_today + stats.pending_today)) * 100 : 0;

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
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

      {/* 统计面板 */}
      {showStats && stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
          <StatCard label="今日已完成" value={stats.completed_today} />
          <StatCard label="今日待回顾" value={stats.pending_today} />
          <StatCard label="累计回顾" value={stats.total_reviews} />
          <StatCard label="连续天数" value={`${stats.streak_days} 天`} />
        </div>
      )}

      {/* 进度条 */}
      {stats && stats.pending_today + stats.completed_today > 0 && (
        <div className="mb-6">
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
        /* 全部回顾完成 */
        <EmptyState
          icon={<CheckCircle size={48} className="text-green-500" />}
          title="太棒了！今日回顾已全部完成 🎉"
        />
      ) : currentReview ? (
        /* 回顾卡片 */
        <div className="max-w-2xl mx-auto">
          <div className="bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)] p-6 shadow-card">
            {/* 笔记标题和元信息 */}
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-heading font-bold text-[var(--color-text)]">
                {currentReview.note_title}
              </h2>
              <span className="text-xs text-[var(--color-text-tertiary)]">
                第 {currentReview.review_count + 1} 次回顾 · 间隔 {currentReview.interval_days} 天
              </span>
            </div>

            {/* 操作按钮 */}
            <div className="flex items-center gap-2 mb-5">
              <button
                onClick={() => navigate(`/notes/${currentReview.note_id}`)}
                className="flex items-center gap-1 text-sm text-[var(--color-accent)] hover:underline"
              >
                <BookOpen size={14} />
                查看原文
              </button>
            </div>

            {/* 掌握程度评分 */}
            <p className="text-sm text-[var(--color-text-secondary)] mb-3">
              你对这篇笔记的掌握程度如何？
            </p>
            <div className="flex flex-wrap gap-2">
              {qualityOptions.map((opt) => (
                <button
                  key={opt.value}
                  disabled={completing}
                  onClick={() => handleComplete(opt.value)}
                  className={`
                    px-3 py-1.5 text-sm rounded-full border transition-all
                    hover:scale-105 disabled:opacity-50
                    ${opt.color}
                  `}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* 跳过 / 下一题 */}
          <div className="flex items-center justify-between mt-4 px-2">
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
