/**
 * 全局搜索模态框（v2 阶段四，Cmd/Ctrl+K 唤起）
 *
 * 数据源策略（不动后端，前端聚合）：
 * - 笔记：语义搜索 notesApi.search（检索质量最高，第一阶段核心）
 * - 会话：sessionsApi.list() 全量拉取后客户端按标题过滤
 * - 知识库：knowledgeApi.listDocuments() 客户端按文件名过滤
 *
 * 交互：顶部搜索输入 + 分类 Tab；Escape / 点击遮罩关闭。
 * 结果点击：笔记 → 跳转 /notes/:id；会话 → 打开 AI 浮层历史视图；知识库 → 跳转 /knowledge。
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Search, FileText, MessageSquare, Library, X } from 'lucide-react';
import { notesApi } from '../../api/notes';
import { sessionsApi } from '../../api/sessions';
import { knowledgeApi } from '../../api/knowledge';
import { useDebounce } from '../../hooks/useDebounce';
import { noteExcerpt } from '../../utils/noteFormat';
import type { Note, ChatSession, KnowledgeDocument } from '../../types/api';
import type { AssistantView } from './AIAssistantPanel';

type SearchTab = 'notes' | 'sessions' | 'knowledge';

interface GlobalSearchModalProps {
  onClose: () => void;
  /** 会话结果点击 → 打开 AI 浮层历史视图 */
  onOpenAssistant: (view: AssistantView) => void;
}

export default function GlobalSearchModal({ onClose, onOpenAssistant }: GlobalSearchModalProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [tab, setTab] = useState<SearchTab>('notes');
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebounce(query, 300);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 结果数据
  const [noteResults, setNoteResults] = useState<Note[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);

  // 打开时聚焦输入框；懒加载会话与知识库全量数据（客户端过滤用）
  useEffect(() => {
    inputRef.current?.focus();
    sessionsApi
      .list()
      .then((res) => setSessions(res.data?.data?.sessions ?? []))
      .catch(() => {});
    knowledgeApi
      .listDocuments()
      .then((res) => setDocuments(res.data?.data?.documents ?? []))
      .catch(() => {});
  }, []);

  // 笔记语义搜索（防抖）
  useEffect(() => {
    if (tab !== 'notes') return;
    if (!debouncedQuery.trim()) {
      setNoteResults([]);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    notesApi
      .search({ query: debouncedQuery, top_k: 20 })
      .then((res) => {
        if (!cancelled) setNoteResults((res.data.data?.results ?? []).map((r) => r.note));
      })
      .catch(() => {
        // 搜索失败静默处理
      })
      .finally(() => {
        if (!cancelled) setSearching(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, debouncedQuery]);

  // 客户端过滤
  const filteredSessions = sessions.filter((s) =>
    s.title.toLowerCase().includes(query.toLowerCase())
  );
  const filteredDocs = documents.filter((d) =>
    d.filename.toLowerCase().includes(query.toLowerCase())
  );

  /** 关闭并跳转 */
  const go = (path: string) => {
    onClose();
    navigate(path);
  };

  const tabs: { key: SearchTab; labelKey: string }[] = [
    { key: 'notes', labelKey: 'gsearch.tabNotes' },
    { key: 'sessions', labelKey: 'gsearch.tabSessions' },
    { key: 'knowledge', labelKey: 'gsearch.tabKnowledge' },
  ];

  const isEmpty =
    (tab === 'notes' && noteResults.length === 0) ||
    (tab === 'sessions' && filteredSessions.length === 0) ||
    (tab === 'knowledge' && filteredDocs.length === 0);

  return (
    <div
      className="fixed inset-0 z-[95] flex items-start justify-center pt-24 bg-black/20 animate-fade-in"
      onClick={onClose}
    >
      <div
        className="w-[560px] max-w-[90vw] rounded-[var(--radius-lg)] bg-[var(--color-card)]
          border border-[var(--color-border)] shadow-[var(--shadow-dialog)] overflow-hidden animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 搜索输入框 */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-[var(--color-border)]">
          <Search size={16} className="text-[var(--color-text-tertiary)] shrink-0" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('gsearch.placeholder')}
            className="flex-1 text-sm bg-transparent text-[var(--color-text)]
              placeholder:text-[var(--color-text-tertiary)] focus:outline-none"
          />
          <button
            onClick={onClose}
            className="p-1 rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-text)]"
            title="Esc"
          >
            <X size={15} />
          </button>
        </div>

        {/* 分类 Tab */}
        <div className="flex gap-1 px-4 py-2 border-b border-[var(--color-border)]">
          {tabs.map((item) => (
            <button
              key={item.key}
              onClick={() => setTab(item.key)}
              className={`px-3 py-1 rounded-full text-xs font-medium transition-colors duration-150 ${
                tab === item.key
                  ? 'bg-[var(--color-accent)] text-[var(--color-on-accent)]'
                  : 'bg-[var(--color-accent-bg)] text-[var(--color-accent)] hover:opacity-80'
              }`}
            >
              {t(item.labelKey)}
            </button>
          ))}
        </div>

        {/* 结果列表 */}
        <div className="max-h-[360px] overflow-y-auto">
          {searching && tab === 'notes' ? (
            <p className="text-center text-sm text-[var(--color-text-tertiary)] py-10">
              {t('gsearch.searching')}
            </p>
          ) : !query.trim() ? (
            <p className="text-center text-sm text-[var(--color-text-tertiary)] py-10">
              {t('gsearch.hint')}
            </p>
          ) : isEmpty ? (
            <p className="text-center text-sm text-[var(--color-text-tertiary)] py-10">
              {t('gsearch.empty')}
            </p>
          ) : tab === 'notes' ? (
            noteResults.map((note) => (
              <button
                key={note.id}
                onClick={() => go(`/notes/${note.id}`)}
                className="w-full flex items-start gap-2.5 px-4 py-3 text-left
                  hover:bg-[var(--color-surface-alt)] transition-colors duration-150"
              >
                <FileText size={15} className="text-[var(--color-text-tertiary)] mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-[var(--color-text)] truncate">{note.title}</p>
                  <p className="text-xs text-[var(--color-text-secondary)] line-clamp-1">
                    {noteExcerpt(note.content, 60)}
                  </p>
                </div>
                {note.category && (
                  <span className="text-[10px] text-[var(--color-text-tertiary)] shrink-0">
                    {note.category}
                  </span>
                )}
              </button>
            ))
          ) : tab === 'sessions' ? (
            filteredSessions.map((session) => (
              <button
                key={session.id}
                onClick={() => {
                  onClose();
                  onOpenAssistant('history');
                }}
                className="w-full flex items-center gap-2.5 px-4 py-3 text-left
                  hover:bg-[var(--color-surface-alt)] transition-colors duration-150"
              >
                <MessageSquare size={15} className="text-[var(--color-text-tertiary)] shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-[var(--color-text)] truncate">{session.title}</p>
                  <p className="text-xs text-[var(--color-text-tertiary)]">
                    {new Date(session.updated_at).toLocaleDateString()}
                  </p>
                </div>
              </button>
            ))
          ) : (
            filteredDocs.map((doc) => (
              <button
                key={doc.id}
                onClick={() => go('/knowledge')}
                className="w-full flex items-center gap-2.5 px-4 py-3 text-left
                  hover:bg-[var(--color-surface-alt)] transition-colors duration-150"
              >
                <Library size={15} className="text-[var(--color-text-tertiary)] shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-[var(--color-text)] truncate">{doc.filename}</p>
                  <p className="text-xs text-[var(--color-text-tertiary)]">
                    {doc.chunk_count} {t('panel.chunks')}
                  </p>
                </div>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
