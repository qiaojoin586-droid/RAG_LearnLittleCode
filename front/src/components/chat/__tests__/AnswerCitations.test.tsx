// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import client from '../../../api/client';
import '../../../i18n';
import { dispatchSSEEvents } from '../../../utils/sseParser';
import MessageContent from '../MessageContent';
import AnswerCitations from '../AnswerCitations';

vi.mock('../../../api/client', () => ({ default: { get: vi.fn() } }));
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

it('reads only a committed reference and requests fresh authorization each time it is opened', async () => {
  const container = document.createElement('div');
  const root = createRoot(container);
  const callback = vi.fn();
  dispatchSSEEvents([{ type: 'response', content: '[E99]' },
    { type: 'done', session_id: 's', reply_message_id: 5, citation_ids: ['E1', 'E1', 'bad'] }], { onCitations: callback });
  expect(callback).toHaveBeenCalledExactlyOnceWith('s', 5, ['E1']);
  vi.mocked(client.get).mockResolvedValueOnce({ data: { data: {
    title: 'Guide', content: '<b>Original</b>', source_locator: { page_number: 2, char_start: 0, char_end: 15 },
  } } }).mockRejectedValueOnce({ isAxiosError: true, response: { status: 404 } });
  try {
    await act(async () => root.render(<>
      <MessageContent content="Result [E1]. [**E99**](https://example.invalid/forged)" />
      <AnswerCitations session="s" message={5} ids={callback.mock.calls[0][2]} />
    </>));
    expect(client.get).not.toHaveBeenCalled();
    expect(container.querySelector('a')).toBeNull();
    const buttons = container.querySelectorAll('button');
    expect(buttons).toHaveLength(1);
    await act(async () => buttons[0].click());
    expect(client.get).toHaveBeenCalledWith('/api/v1/chat/s/messages/5/citations/E1', { signal: expect.any(AbortSignal) });
    expect(container.querySelector('blockquote')?.textContent).toBe('<b>Original</b>');
    expect(container.querySelector('figcaption')?.textContent).toContain('第 2 页');
    expect(container.querySelector('b')).toBeNull();
    await act(async () => buttons[0].click());
    expect(container.querySelector('blockquote')).toBeNull();
    await act(async () => buttons[0].click());
    expect(client.get).toHaveBeenCalledTimes(2);
    expect(container.textContent).not.toContain('Original');
    expect(container.querySelector('[role=status]')?.textContent).toContain('不再可见');
  } finally {
    await act(async () => root.unmount());
  }
});
