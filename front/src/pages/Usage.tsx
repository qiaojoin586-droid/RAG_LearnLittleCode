/**
 * 用量与费用面板
 *
 * 数据来源：GET /api/v1/usage/summary（按当前用户聚合 model_traces）
 * 展示：调用次数 / Token 总量 / 费用 / 平均延迟 + 按模型、按阶段明细
 *
 * 说明：费用按模型单价计算（model_pricing 表），未配置价格的模型费用为 0；
 * 因此换模型后需同步在 config/pricing.yaml 增加同名条目。
 */

import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Activity, Coins, Cpu, Gauge } from 'lucide-react';

import { usageApi } from '../api/usage';
import type { UsageSummary } from '../types/api';

/** 可选统计区间（天） */
const RANGES: { days: number; key: string }[] = [
  { days: 1, key: 'usage.range.today' },
  { days: 7, key: 'usage.range.week' },
  { days: 30, key: 'usage.range.month' },
];

/** 后端 stage 标识 → i18n 键（未知阶段直接展示原值） */
const STAGE_LABELS: Record<string, string> = {
  chat: 'usage.stage.chat',
  agent: 'usage.stage.agent',
  plan_execute: 'usage.stage.plan',
  classify: 'usage.stage.classify',
  title: 'usage.stage.title',
  summary: 'usage.stage.summary',
  rag: 'usage.stage.rag',
  autocomplete: 'usage.stage.autocomplete',
  auto_tag: 'usage.stage.autoTag',
  write_assistant: 'usage.stage.writeAssist',
  review_question: 'usage.stage.reviewQuestion',
};

export default function Usage() {
  const { t } = useTranslation();
  const [days, setDays] = useState(7);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (d: number) => {
    setLoading(true);
    try {
      const res = await usageApi.getSummary(d);
      setData(res.data.data);
    } catch {
      toast.error(t('usage.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load(days);
  }, [days, load]);

  const stageLabel = (stage: string) => {
    const key = STAGE_LABELS[stage];
    return key ? t(key) : stage;
  };

  const statCard = (
    icon: React.ReactNode,
    label: string,
    value: string,
    sub?: string,
  ) => (
    <div className="p-4 rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)] shadow-card">
      <div className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)] mb-1">
        {icon}
        {label}
      </div>
      <div className="text-xl font-heading font-bold text-[var(--color-text)]">{value}</div>
      {sub && <div className="text-xs text-[var(--color-text-tertiary)] mt-0.5">{sub}</div>}
    </div>
  );

  const empty = !loading && data && data.total_calls === 0;

  return (
    <div className="p-6 h-full flex flex-col overflow-hidden">
      {/* 头部：标题 + 区间切换 */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <h1 className="text-2xl font-heading font-bold text-[var(--color-text)]">
          {t('usage.title')}
        </h1>
        <div className="flex items-center rounded-[var(--radius-md)] border border-[var(--color-border)] overflow-hidden">
          {RANGES.map((r) => (
            <button
              key={r.days}
              type="button"
              onClick={() => setDays(r.days)}
              className={`px-3 py-1.5 text-sm transition-all duration-200 ${
                days === r.days
                  ? 'bg-[var(--color-accent)] text-[var(--color-on-accent)]'
                  : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
              }`}
            >
              {t(r.key)}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {loading ? (
          <p className="text-center text-[var(--color-text-tertiary)]">{t('common.loading')}</p>
        ) : empty ? (
          <p className="text-center text-[var(--color-text-tertiary)] mt-10">{t('usage.empty')}</p>
        ) : data ? (
          <>
            {/* 汇总卡片 */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
              {statCard(<Activity size={14} />, t('usage.calls'), String(data.total_calls))}
              {statCard(
                <Cpu size={14} />,
                t('usage.tokens'),
                data.total_tokens.toLocaleString(),
                `${t('usage.tokensInOut')} ${data.total_prompt_tokens.toLocaleString()} / ${data.total_completion_tokens.toLocaleString()}`,
              )}
              {statCard(
                <Coins size={14} />,
                t('usage.cost'),
                data.total_cost_cny.toFixed(4),
              )}
              {statCard(
                <Gauge size={14} />,
                t('usage.avgLatency'),
                `${Math.round(data.avg_latency_ms)} ms`,
              )}
            </div>

            {/* 按模型 */}
            <h2 className="text-sm font-semibold text-[var(--color-text)] mb-2">
              {t('usage.byModel')}
            </h2>
            <div className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden mb-5">
              <table className="w-full text-sm">
                <thead className="bg-[var(--color-surface-alt)] text-xs text-[var(--color-text-tertiary)]">
                  <tr>
                    <th className="text-left px-4 py-2 font-normal">{t('usage.model')}</th>
                    <th className="text-right px-4 py-2 font-normal">{t('usage.calls')}</th>
                    <th className="text-right px-4 py-2 font-normal">{t('usage.cost')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_model.map((m) => (
                    <tr key={m.model} className="border-t border-[var(--color-border)]">
                      <td className="px-4 py-2 text-[var(--color-text)] break-all">{m.model}</td>
                      <td className="px-4 py-2 text-right text-[var(--color-text-secondary)]">{m.calls}</td>
                      <td className="px-4 py-2 text-right text-[var(--color-text-secondary)]">
                        {m.cost_cny.toFixed(4)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 按阶段 */}
            <h2 className="text-sm font-semibold text-[var(--color-text)] mb-2">
              {t('usage.byStage')}
            </h2>
            <div className="rounded-[var(--radius-lg)] border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-[var(--color-surface-alt)] text-xs text-[var(--color-text-tertiary)]">
                  <tr>
                    <th className="text-left px-4 py-2 font-normal">{t('usage.stage')}</th>
                    <th className="text-right px-4 py-2 font-normal">{t('usage.calls')}</th>
                    <th className="text-right px-4 py-2 font-normal">{t('usage.tokensInOut')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_stage.map((s) => (
                    <tr key={s.stage} className="border-t border-[var(--color-border)]">
                      <td className="px-4 py-2 text-[var(--color-text)]">{stageLabel(s.stage)}</td>
                      <td className="px-4 py-2 text-right text-[var(--color-text-secondary)]">{s.calls}</td>
                      <td className="px-4 py-2 text-right text-[var(--color-text-secondary)]">
                        {s.prompt_tokens.toLocaleString()} / {s.completion_tokens.toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
