import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import axios from 'axios';
import client from '../../api/client';
import { endpoints } from '../../api/endpoints';
import type { ApiResponse, CitationSource } from '../../types/api';

function SourceExcerpt({ url }: { url: string }) {
  const { t } = useTranslation();
  const [source, setSource] = useState<CitationSource>();
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    client.get<ApiResponse<CitationSource>>(url, { signal: controller.signal })
      .then(({ data }) => { if (!controller.signal.aborted) setSource(data.data); })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(axios.isAxiosError(cause) && [403, 404].includes(cause.response?.status ?? 0)
            ? 'chat.citation.unavailable' : 'chat.citation.retry');
        }
      });
    return () => controller.abort();
  }, [url]);
  if (!source) return <p className="py-2" role="status">{t(error || 'chat.citation.loading')}</p>;
  const span = source.source_locator;
  return (
    <figure className="mt-2 border-l-2 pl-2 text-sm">
      <figcaption className="break-words font-medium">
        {source.title}
        {span.page_number != null && <> · {t('chat.citation.page', { page: span.page_number })}</>}
        {span.section_title && <> · {span.section_title}</>}
        {' · '}{t('chat.citation.range', { start: span.char_start + 1, end: span.char_end })}
      </figcaption>
      <blockquote className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words">{source.content}</blockquote>
    </figure>
  );
}

export default function AnswerCitations({ session, message, ids }: { session: string; message: number; ids: string[] }) {
  const { t } = useTranslation();
  const [active, setActive] = useState<string>();
  const panel = useId();
  return (
    <div className="mt-2 text-xs">
      <div className="flex flex-wrap items-center gap-1">
        <span>{t('chat.citation.sources')}</span>
        {ids.map((id) => (
          <button key={id} type="button" aria-label={t('chat.citation.open', { id })}
            aria-expanded={active === id} aria-controls={panel}
            className="rounded px-2 py-1 text-[var(--color-accent)] hover:underline focus-visible:outline"
            onClick={() => setActive(active === id ? undefined : id)}>
            [{id}]
          </button>
        ))}
      </div>
      <div id={panel}>
        {active && <SourceExcerpt key={active} url={endpoints.chat.citation(session, message, active)} />}
      </div>
    </div>
  );
}
