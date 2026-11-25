// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { AxiosHeaders } from 'axios';
import { expect, it, vi } from 'vitest';
import { usageApi } from '../../api/usage';
import Usage from '../Usage';
import '../../i18n';

vi.mock('../../api/usage', () => ({ usageApi: { getSummary: vi.fn() } }));
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

it('displays decimal amounts per currency and shows configuration tests without business calls', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  const currencyTotals = [
    { currency: 'USD', priced_amount: '0.0000000001', legacy_estimated_amount: null,
      unknown_usage_count: 0, unknown_price_count: 0, legacy_estimate_count: 0 },
    { currency: 'CNY', priced_amount: '0.0000000000', legacy_estimated_amount: null,
      unknown_usage_count: 0, unknown_price_count: 0, legacy_estimate_count: 0 },
  ];
  vi.mocked(usageApi.getSummary).mockResolvedValue({ status: 200, statusText: 'OK', headers: {},
    config: { headers: new AxiosHeaders() }, data: { code: 0, message: 'ok', request_id: 'usage-fixture', data: {
    days: 7, total_calls: 0, total_tokens: 0, total_prompt_tokens: 0, total_completion_tokens: 0,
    total_cost_cny: null, avg_latency_ms: 0, by_stage: [], by_model: [], currency_totals: [],
    config_test_usage: { calls: 2, known_prompt_tokens: 40, known_completion_tokens: 10,
      cost_amount: null, cost_status: 'complete', currency_totals: currencyTotals },
  } } });
  try {
    await act(async () => root.render(<Usage />));
    expect(container.textContent).toContain('USD 0.0000000001');
    expect(container.textContent).toContain('CNY 0');
    expect(container.textContent).toMatch(/费用未知|Cost unknown/);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
