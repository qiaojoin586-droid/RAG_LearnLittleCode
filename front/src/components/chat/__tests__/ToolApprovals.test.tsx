// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import client from '../../../api/client';
import '../../../i18n';
import ToolApprovals from '../ToolApprovals';

vi.mock('../../../api/client', () => ({ default: { get: vi.fn(), post: vi.fn() } }));
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

let container: HTMLDivElement;
let root: Root;
const proposal = {
  operation_id: 'op-one', preview_hash: 'a'.repeat(64), tool_name: 'update_note_tool',
  arguments: { content: '<script>plain text</script>' }, target: { id: 'note-one', title: 'Current note' },
  expires_at: '2026-11-19T05:00:00Z',
};

function mockLists(approvals: object[], emails: object[] = []) {
  vi.mocked(client.get).mockImplementation(async (url) => ({
    data: { data: url.endsWith('/email-operations') ? emails : approvals },
  }));
}

beforeEach(() => {
  vi.resetAllMocks();
  container = document.body.appendChild(document.createElement('div'));
  root = createRoot(container);
  mockLists([proposal]);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

function button(text: string) {
  return [...container.querySelectorAll('button')].find((item) => item.textContent === text)!;
}

it('shows the exact content as text and only sends the decision with its preview hash', async () => {
  await act(async () => root.render(<ToolApprovals sessionId="session-one" streaming={false} />));
  container.querySelector('details')!.open = true;
  expect(container.textContent).toContain(proposal.arguments.content);
  expect(container.textContent).toContain('Current note');
  expect(container.querySelector('script')).toBeNull();
  expect(client.post).not.toHaveBeenCalled();
  mockLists([]);
  vi.mocked(client.post).mockResolvedValue({ data: { data: { status: 'executed' } } });
  await act(async () => button('确认执行').click());
  expect(client.post).toHaveBeenCalledExactlyOnceWith('/api/v1/chat/approvals/op-one', {
    decision: 'approve', preview_hash: proposal.preview_hash,
  });
  expect(container.querySelector('[role=status]')?.textContent).toBe('操作已执行。');
});

it('keeps the same approval available after an unconfirmed response', async () => {
  vi.mocked(client.post).mockRejectedValueOnce(new Error('network'))
    .mockResolvedValueOnce({ data: { data: { status: 'rejected' } } });
  await act(async () => root.render(<ToolApprovals sessionId="session-one" streaming={false} />));
  container.querySelector('details')!.open = true;
  await act(async () => button('拒绝').click());
  expect(container.querySelector('[role=alert]')).not.toBeNull();
  mockLists([]);
  await act(async () => button('拒绝').click());
  expect(vi.mocked(client.post).mock.calls[0]).toEqual(vi.mocked(client.post).mock.calls[1]);
  expect(container.querySelector('[role=status]')?.textContent).toBe('操作已拒绝。');
});

it('previews SMTP parameters and provides manual receipt verification for an unknown outcome', async () => {
  const email = { ...proposal, tool_name: 'send_email', target: {}, arguments: {
    to: 'fixture@example.invalid', subject: 'Approved subject', body: 'Approved mail body', format: 'pdf',
  } };
  const receipt = { operation_id: email.operation_id, status: 'unknown', result: {
    to: email.arguments.to, subject: email.arguments.subject, message_id: '<op-one@raglearn.local>',
  } };
  mockLists([email]);
  await act(async () => root.render(<ToolApprovals sessionId="session-one" streaming={false} />));
  container.querySelector('details')!.open = true;
  expect(container.textContent).toContain('fixture@example.invalid');
  expect(container.textContent).toContain('PDF 附件');
  expect(container.textContent).toContain('Approved mail body');
  expect(client.post).not.toHaveBeenCalled();
  mockLists([], [receipt]);
  vi.mocked(client.post).mockResolvedValue({ data: { data: receipt } });
  await act(async () => button('确认发送').click());
  expect(vi.mocked(client.post).mock.calls[0]).toEqual(['/api/v1/chat/approvals/op-one', {
    decision: 'approve', preview_hash: email.preview_hash,
  }]);
  expect(container.textContent).toContain('<op-one@raglearn.local>');
  expect(container.textContent).toContain('待核对');
  mockLists([], [{ ...receipt, status: 'received' }]);
  await act(async () => button('我已核实收件').click());
  expect(vi.mocked(client.post).mock.calls[1]).toEqual(['/api/v1/chat/approvals/op-one/email-received']);
  expect(container.querySelector('[role=status]')?.textContent).toContain('已核实收件');
  expect(button('确认发送')).toBeUndefined();
});
