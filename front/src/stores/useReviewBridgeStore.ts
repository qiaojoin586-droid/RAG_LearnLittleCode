/**
 * 回顾页与侧边栏桥接（v2 每日回顾两栏布局）
 *
 * 修复「重复笔记列表」后，SidePanel 的「每日回顾」列表成为页面唯一笔记选择入口：
 * - 侧边栏点击条目 → selectReview(review_id)，DailyReview 订阅后
 *   同步切换答题卡片（currentIndex）与预览区内容
 * - 页面完成回顾 → bumpList()，SidePanel 订阅后重新拉取列表（保持最新）
 *
 * selectSeq：同一条目重复点击也会触发页面同步（Zustand 相同值不通知，故用序号驱动）
 */

import { create } from 'zustand';

interface ReviewBridgeState {
  /** 侧边栏当前选中的回顾条目 ID（null = 未选中） */
  selectedReviewId: number | null;
  /** 选择序号（每次点击自增，同条目重复点击也触发同步） */
  selectSeq: number;
  /** 列表刷新信号（完成回顾后自增，侧边栏重新拉取） */
  listVersion: number;
  selectReview: (reviewId: number) => void;
  bumpList: () => void;
}

export const useReviewBridgeStore = create<ReviewBridgeState>()((set) => ({
  selectedReviewId: null,
  selectSeq: 0,
  listVersion: 0,

  selectReview: (reviewId) =>
    set((s) => ({ selectedReviewId: reviewId, selectSeq: s.selectSeq + 1 })),

  bumpList: () => set((s) => ({ listVersion: s.listVersion + 1 })),
}));
