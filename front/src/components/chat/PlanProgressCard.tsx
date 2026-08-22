/**
 * PlanProgressCard - 执行计划进度卡片组件
 * 
 * 在 Plan-and-Execute 模式下展示执行计划的进度：
 * - 计划目标和总步骤数
 * - 每个步骤的状态（pending/running/completed/failed）
 * - 进度条
 */

import { CheckCircle2, Circle, Loader2, XCircle, ChevronDown, ChevronUp, Wrench } from 'lucide-react';
import { useState } from 'react';

/** 步骤状态 */
export type StepStatus = 'pending' | 'running' | 'completed' | 'failed';

/** 步骤数据 */
export interface PlanStepData {
  step: number;
  action: string;
  status: StepStatus;
  result?: string;
  /** 当前正在执行的工具名 */
  activeTool?: string;
}

interface PlanProgressCardProps {
  /** 计划目标 */
  goal: string;
  /** 步骤列表 */
  steps: PlanStepData[];
  /** 已完成步骤数 */
  completedSteps: number;
  /** 总步骤数 */
  totalSteps: number;
  /** 是否已完成 */
  isComplete?: boolean;
  /** 是否折叠（默认完成后折叠） */
  defaultCollapsed?: boolean;
  /** 当前活跃工具名（全局级别，显示在正在执行的步骤下） */
  currentTool?: string;
  /** 反思阶段（L1 综合后自检）：'' | 'checking' | 'refining' */
  reflectStatus?: string;
  /** 反思修正轮次 */
  reflectRound?: number;
}

const statusIcons: Record<StepStatus, React.ReactNode> = {
  pending: <Circle className="w-4 h-4 text-gray-400" />,
  running: <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />,
  completed: <CheckCircle2 className="w-4 h-4 text-green-500" />,
  failed: <XCircle className="w-4 h-4 text-red-500" />,
};

const statusLabels: Record<StepStatus, string> = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
};

export default function PlanProgressCard({
  goal,
  steps,
  completedSteps,
  totalSteps,
  isComplete = false,
  defaultCollapsed,
  currentTool,
  reflectStatus,
  reflectRound = 0,
}: PlanProgressCardProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed ?? isComplete);
  
  const progress = totalSteps > 0 ? (completedSteps / totalSteps) * 100 : 0;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-3 mb-2">
      {/* 头部：目标 + 折叠按钮 */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="flex items-center justify-between w-full text-left gap-2"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-medium text-[var(--color-text)] truncate">
            📋 {goal}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs text-[var(--color-text-secondary)]">
            {completedSteps}/{totalSteps}
          </span>
          {collapsed ? (
            <ChevronDown className="w-4 h-4 text-[var(--color-text-secondary)]" />
          ) : (
            <ChevronUp className="w-4 h-4 text-[var(--color-text-secondary)]" />
          )}
        </div>
      </button>

      {/* 进度条 */}
      <div className="mt-2 h-1.5 bg-[var(--color-bg)] rounded-full overflow-hidden">
        <div
          className="h-full bg-[var(--color-accent)] rounded-full transition-all duration-500 ease-out"
          style={{ width: `${progress}%` }}
        />
      </div>

      {/* 步骤列表（可折叠） */}
      {!collapsed && (
        <div className="mt-3 space-y-1.5">
          {steps.map((step) => (
            <div
              key={step.step}
              className="flex items-start gap-2 text-sm"
            >
              <span className="mt-0.5 shrink-0">
                {statusIcons[step.status]}
              </span>
              <div className="min-w-0 flex-1">
                <span
                  className={
                    step.status === 'completed'
                      ? 'text-[var(--color-text-secondary)] line-through'
                      : step.status === 'running'
                      ? 'text-[var(--color-accent)] font-medium'
                      : 'text-[var(--color-text)]'
                  }
                >
                  步骤{step.step}：{step.action}
                </span>
                {step.status === 'running' && (
                  <span className="ml-2 text-xs text-[var(--color-text-secondary)]">
                    {statusLabels.running}
                    {(step.activeTool || currentTool) ? (
                      <span className="inline-flex items-center gap-1 ml-1 text-blue-400">
                        <Wrench className="w-3 h-3" />
                        {step.activeTool || currentTool}
                      </span>
                    ) : '...'}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 反思状态提示（L1 综合后自检 + L2 失败修复，AI对话Reflection反思机制设计方案.md §3.8） */}
      {reflectStatus === 'checking' && (
        <div className="mt-2 text-xs text-blue-500 dark:text-blue-400">
          🔍 正在反思回答质量…
        </div>
      )}
      {reflectStatus === 'repairing' && (
        <div className="mt-2 text-xs text-amber-500 dark:text-amber-400">
          🔧 执行遇到问题，正在自动修复重试…
        </div>
      )}
      {reflectStatus === 'refining' && (
        <div className="mt-2 text-xs text-amber-500 dark:text-amber-400">
          🔄 正在修正回答{reflectRound > 0 ? `（第 ${reflectRound} 轮）` : ''}…
        </div>
      )}

      {/* 完成提示 */}
      {isComplete && collapsed && (
        <div className="mt-2 text-xs text-green-600 dark:text-green-400">
          ✅ 执行计划已完成
        </div>
      )}
    </div>
  );
}
