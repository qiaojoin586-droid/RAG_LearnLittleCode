/**
 * 笔记编辑器（v2 内联组件版）
 *
 * 从全屏路由页改造为可复用内嵌组件，嵌入笔记页三栏的右侧详情区：
 * - 接收 note 对象（列表已含完整数据，免重复请求）；null = 新建模式
 * - 移除路由依赖（useParams / useNavigate）与「返回」按钮（列表与详情同屏）
 * - 编辑/预览分栏、格式工具栏、标签管理、分类选择、置顶、导出、保存全部原样保留
 *
 * 父组件以 key={note?.id ?? 'new'} 强制按笔记重挂载，切换笔记时状态自然重置。
 * 编辑内容高度：编辑器块 flex-1 填充第三栏剩余高度（原 calc(100vh-380px) 适配内联场景）。
 */

import { useState, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { ArrowLeft, Save, Download, Bold, Italic, Heading1, Heading2, Code, List, ListOrdered, Quote, Pencil, Columns, Eye } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github.css';
import { notesApi } from '../api/notes';
import { useAutocomplete } from '../hooks/useAutocomplete';
import { GhostTextArea } from '../components/GhostTextArea';
import TagInput from '../components/common/TagInput';
import { useCategoryStore } from '../stores/useCategoryStore';
import { flattenCategories } from '../types/category';
import { isHtml, htmlToMarkdown, normalizeNoteContent, escapeInlineHtml } from '../utils/markdownCompat';
import { detectNoteFormat, type NoteFormat } from '../utils/noteFormat';
import type { Note } from '../types/api';

type EditorMode = 'edit' | 'split' | 'preview';

interface NoteEditorProps {
  /** 要编辑的笔记（列表已加载完整数据）；null = 新建模式 */
  note: Note | null;
  /** 新建模式的文档类型（/notes/new?format= 参数；已有笔记读 note.format） */
  initialFormat?: 'md' | 'txt';
  /** 新建保存成功后回调（父级刷新列表并切换选中） */
  onCreated: (note: Note) => void;
  /** 更新保存成功后回调（父级同步列表卡片数据） */
  onUpdated: (note: Note) => void;
  /** 移动端返回列表（桌面端无返回按钮，列表与详情同屏） */
  onBackMobile?: () => void;
}

export default function NoteEditor({ note, initialFormat, onCreated, onUpdated, onBackMobile }: NoteEditorProps) {
  const { t } = useTranslation();

  // 文档类型：已有笔记读 note.format；新建模式读 URL 参数；缺失/未知兜底 'md'
  const noteFormat: NoteFormat = detectNoteFormat(note?.format ?? initialFormat);
  const isTxt = noteFormat === 'txt';

  // 父组件以 key 重挂载，直接用 props 初始化状态（切换笔记无残留）
  const [title, setTitle] = useState(note?.title ?? '');
  const [content, setContent] = useState(() => {
    const raw = note?.content ?? '';
    // 兼容旧数据：如果内容是 HTML，自动转换为 Markdown；
    // 再归一化（剥外层围栏/CRLF），避免整篇被渲染为代码块
    return normalizeNoteContent(isHtml(raw) ? htmlToMarkdown(raw) : raw);
  });
  const [tags, setTags] = useState<string[]>(note?.tags ?? []);
  // 分类改为按 category_id 关联（后端双写同步 category 字符串）；
  // 空串 = 未分类（显式传 null 给后端清除关联）
  const [categoryId, setCategoryId] = useState(note?.category_id ?? '');
  const { categories, fetchCategories } = useCategoryStore();
  const [isPinned, setIsPinned] = useState(note?.is_pinned ?? false);
  const [editorMode, setEditorMode] = useState<EditorMode>('split');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  /** 保存笔记（更新 / 新建，业务逻辑与原全屏页一致） */
  const handleSave = async () => {
    if (!title.trim() || !content.trim()) {
      toast.error('标题和内容不能为空');
      return;
    }
    try {
      if (note) {
        const res = await notesApi.update(note.id, {
          title, content, tags, category_id: categoryId || null, is_pinned: isPinned,
        });
        toast.success('保存成功');
        onUpdated(res.data.data);
      } else {
        // 新建时写入所选文档类型（md/txt）
        const res = await notesApi.create({
          title, content, tags, category_id: categoryId || undefined, is_pinned: isPinned,
          format: noteFormat,
        });
        toast.success('创建成功');
        onCreated(res.data.data);
      }
    } catch {
      toast.error(t('common.error'));
    }
  };

  /** 导出为 Markdown 文件 */
  const handleExport = () => {
    if (!title.trim()) {
      toast.error('标题不能为空');
      return;
    }
    // 导出按文档类型适配：md → `# 标题` + Markdown 正文；txt → 纯正文
    const ext = isTxt ? 'txt' : 'md';
    const fileContent = isTxt ? content : `# ${title}\n\n${content}`;
    const blob = new Blob([fileContent], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${title.replace(/[\\/:*?"<>|]/g, '_')}.${ext}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast.success('导出成功');
  };

  // ===== AI 联机补全（薄片：建议条 + Tab 采纳，暂不做内联覆盖层）=====
  // 设计文档：docs/AI联机补全功能设计优化方案.md §4
  const isMobile = typeof window !== 'undefined' && window.innerWidth < 768;
  const autocomplete = useAutocomplete({
    debounceMs: 600,
    enabled: !isTxt && !isMobile,   // MVP 仅桌面 md 模式启用（txt / 移动端第二阶段）
  });

  // ===== AI 写作辅助（续写 / 扩写 / 摘要，用户主动触发）=====
  const [aiLoading, setAiLoading] = useState(false);
  const [aiResult, setAiResult] = useState('');

  /** 内容变化：触发补全（有选区时不触发，§5.4） */
  const handleContentChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const newContent = e.target.value;
    setContent(newContent);

    if (e.target.selectionStart !== e.target.selectionEnd) {
      autocomplete.dismiss();
      return;
    }
    const cursor = e.target.selectionStart;
    // F8：后端只用到光标前 800 字符（AUTOCOMPLETE_MAX_CONTEXT），
    // 前端多传无益，这里与后端口径对齐，减少约 30% 请求体
    const before = newContent.slice(Math.max(0, cursor - 800), cursor);
    const after = newContent.slice(cursor, cursor + 300);
    autocomplete.onContentChange(before + after, before.length, cursor, title);
  }, [autocomplete, title]);

  /** 采纳建议：插入到请求时的光标位置（而非追加到末尾） */
  const handleAccept = useCallback(() => {
    const { text, cursorPosition: pos } = autocomplete.accept();
    if (!text) return;
    setContent(prev => prev.slice(0, pos) + text + prev.slice(pos));
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      const newPos = pos + text.length;
      ta.setSelectionRange(newPos, newPos);
    });
  }, [autocomplete]);

  /** 按键：Tab 采纳 / Esc 丢弃 / 其它按键丢弃（IME 组合期间一律放行） */
  const handleEditorKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return;   // 中文输入法选词/取消候选不能被拦截
    if (e.key === 'Tab' && autocomplete.completion) {
      e.preventDefault();
      handleAccept();
    } else if (e.key === 'Escape' && autocomplete.completion) {
      e.preventDefault();
      autocomplete.dismiss();
    } else if (e.key !== 'Tab') {
      autocomplete.dismiss();   // 继续输入 / 光标移动 → 上下文已变，丢弃
    }
  }, [autocomplete, handleAccept]);

  /** 触发 AI 写作辅助（结果先预览，用户确认后才插入，非破坏性） */
  const handleAiWrite = useCallback(async (mode: 'continue' | 'expand' | 'summary') => {
    if (aiLoading) return;
    setAiResult('');
    setAiLoading(true);
    try {
      const res = await notesApi.writeAssistant({ content, mode, title });
      const data = res.data.data;
      if (data.result) {
        setAiResult(data.result);
      } else {
        toast.error(t('note.ai.failed'));
      }
    } catch {
      toast.error(t('note.ai.failed'));
    } finally {
      setAiLoading(false);
    }
  }, [content, title, aiLoading, t]);

  /** 采纳 AI 结果：插入到光标处（不覆盖原文，可 Ctrl+Z 前手动撤销） */
  const handleAiAccept = useCallback(() => {
    const text = aiResult;
    if (!text) return;
    const ta = textareaRef.current;
    const pos = ta ? ta.selectionStart : content.length;
    setContent(prev => prev.slice(0, pos) + text + prev.slice(pos));
    setAiResult('');
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      const newPos = pos + text.length;
      el.setSelectionRange(newPos, newPos);
    });
  }, [aiResult, content.length]);

  /** AI 写作辅助按钮组（md / txt 两个工具栏共用） */
  const renderAiWriteButtons = () => (
    <div className="flex items-center gap-0.5 ml-1 pl-1 border-l border-[var(--color-border)]">
      {(['continue', 'expand', 'summary'] as const).map((m) => (
        <button
          key={m}
          type="button"
          disabled={aiLoading}
          onClick={() => handleAiWrite(m)}
          className="px-1.5 py-1 rounded-lg text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)] disabled:opacity-40 transition-all duration-200"
        >
          {t(`note.ai.${m}`)}
        </button>
      ))}
    </div>
  );

  /** AI 结果预览条（生成中 / 待采纳） */
  const renderAiResultBar = () => {
    if (!aiLoading && !aiResult) return null;
    return (
      <div className="shrink-0 border-t border-[var(--color-border)] bg-[var(--color-surface-alt)] px-3 py-2">
        {aiLoading ? (
          <div className="text-xs text-[var(--color-text-tertiary)]">{t('note.ai.loading')}</div>
        ) : (
          <>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs text-[var(--color-text-tertiary)]">{t('note.ai.result')}</span>
              <div className="ml-auto flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleAiAccept}
                  className="text-xs text-[#1677ff] hover:underline"
                >
                  {t('note.ai.accept')}
                </button>
                <button
                  type="button"
                  onClick={() => setAiResult('')}
                  className="text-xs text-[var(--color-text-tertiary)] hover:underline"
                >
                  {t('note.ai.discard')}
                </button>
              </div>
            </div>
            <div className="max-h-40 overflow-y-auto whitespace-pre-wrap text-sm text-[var(--color-text)]">
              {aiResult}
            </div>
          </>
        )}
      </div>
    );
  };

  /** 向 textarea 插入/包裹选中文本 */
  const wrapSelection = useCallback((before: string, after: string = before) => {
    const ta = textareaRef.current;
    if (!ta) return;
    // ★ F3：工具栏直接 setContent 绕过 onChange → 已显示的补全建议的插入点会失效，
    // 此时按 Tab 会插到错误位置，必须先丢弃。
    autocomplete.dismiss();
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const selected = content.substring(start, end);
    const replacement = `${before}${selected || '文本'}${after}`;
    const newContent = content.substring(0, start) + replacement + content.substring(end);
    setContent(newContent);
    requestAnimationFrame(() => {
      ta.focus();
      const cursorPos = start + before.length;
      ta.setSelectionRange(cursorPos, cursorPos + (selected || '文本').length);
    });
  }, [content, autocomplete]);

  /** 在行首插入前缀 */
  const prefixLine = useCallback((prefix: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    autocomplete.dismiss();   // ★ F3：同 wrapSelection，避免陈旧插入点
    const start = ta.selectionStart;
    // 找到当前行的起始位置
    const lineStart = content.lastIndexOf('\n', start - 1) + 1;
    const newContent = content.substring(0, lineStart) + prefix + content.substring(lineStart);
    setContent(newContent);
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(start + prefix.length, start + prefix.length);
    });
  }, [content, autocomplete]);

  /** 工具栏按钮配置 */
  const toolbarActions = [
    { icon: <Bold size={15} />, title: '加粗', action: () => wrapSelection('**') },
    { icon: <Italic size={15} />, title: '斜体', action: () => wrapSelection('*') },
    { type: 'divider' as const },
    { icon: <Heading1 size={15} />, title: '一级标题', action: () => prefixLine('# ') },
    { icon: <Heading2 size={15} />, title: '二级标题', action: () => prefixLine('## ') },
    { type: 'divider' as const },
    { icon: <Code size={15} />, title: '代码块', action: () => wrapSelection('\n```\n', '\n```\n') },
    { icon: <Quote size={15} />, title: '引用', action: () => prefixLine('> ') },
    { type: 'divider' as const },
    { icon: <List size={15} />, title: '无序列表', action: () => prefixLine('- ') },
    { icon: <ListOrdered size={15} />, title: '有序列表', action: () => prefixLine('1. ') },
  ];

  return (
    <div className="flex flex-col h-full px-4 py-3 overflow-hidden">
      {/* 顶部栏（同行布局优化）：移动端返回 + 标题（左，flex-1）+ 导出/保存（右），垂直居中 */}
      <div className="flex items-center gap-3 mb-3 shrink-0">
        {onBackMobile && (
          <button
            onClick={onBackMobile}
            className="md:hidden flex items-center gap-1 text-[var(--color-text-secondary)]
              hover:text-[var(--color-accent)] transition-colors shrink-0"
          >
            <ArrowLeft size={18} />
            {t('common.back')}
          </button>
        )}
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t('note.title')}
          className="flex-1 min-w-0 text-2xl font-heading font-bold bg-transparent border-none outline-none text-[var(--color-text)]"
        />
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={handleExport}
            className="flex items-center gap-2 px-4 py-2 rounded-xl border border-[var(--color-border)] text-[var(--color-accent)] text-sm hover:bg-[var(--color-accent-bg)] transition-all duration-200"
            title="导出为 Markdown 文件"
          >
            <Download size={16} />
            导出
          </button>
          <button
            onClick={handleSave}
            className="flex items-center gap-2 px-4 py-2 rounded-[999px] bg-[var(--color-accent)] text-[var(--color-on-accent)] text-sm hover:bg-[var(--color-accent-hover)] active:scale-[0.98] transition-all duration-200 shadow-[var(--shadow-accent-md)]"
          >
            <Save size={16} />
            {t('note.save')}
          </button>
        </div>
      </div>

      {/* 标签和分类（随内容区同步拉宽） */}
      <div className="flex flex-wrap gap-4 mb-3 p-3 bg-[var(--color-card)]/80 rounded-xl border border-[var(--color-border)] shadow-[var(--shadow-card)] shrink-0">
        <div className="flex-1 min-w-[200px]">
          <label className="block text-xs text-[var(--color-text-tertiary)] mb-1">{t('note.tags')}</label>
          <TagInput tags={tags} onChange={setTags} />
        </div>
        <div className="w-40">
          <label className="block text-xs text-[var(--color-text-tertiary)] mb-1">{t('note.category')}</label>
          <select
            value={categoryId}
            onChange={(e) => setCategoryId(e.target.value)}
            onFocus={() => categories.length === 0 && fetchCategories()}
            className="w-full px-2 py-1 text-sm rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)] transition-all duration-200"
          >
            <option value="">{t('note.uncategorized')}</option>
            {flattenCategories(categories).map(({ category: c, depth }) => (
              <option key={c.id} value={c.id}>
                {' '.repeat(depth * 3) + (c.icon ? `${c.icon} ` : '') + c.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-end">
          <label className="flex items-center gap-2 text-sm text-[var(--color-text-secondary)] cursor-pointer">
            <input
              type="checkbox"
              checked={isPinned}
              onChange={(e) => setIsPinned(e.target.checked)}
              className="rounded"
            />
            {t('note.pinned')}
          </label>
        </div>
      </div>

      {/* 编辑区：md 双栏（工具栏 + 分栏预览）/ txt 纯文本单栏（文档类型功能） */}
      <div className="flex-1 min-h-[300px] border border-[var(--color-border)] rounded-xl bg-[var(--color-card)] overflow-hidden shadow-[var(--shadow-card)] flex flex-col">
        {/* 工具栏：仅 Markdown 模式显示；纯文本模式仅显示类型标签 */}
        {isTxt ? (
          <div className="flex items-center justify-end gap-2 px-2 py-1.5 border-b border-[var(--color-border)] bg-[var(--color-surface)] shrink-0">
            {renderAiWriteButtons()}
            <span className="text-xs text-[var(--color-text-tertiary)] select-none">{t('note.format.txt')}</span>
          </div>
        ) : (
        <div className="flex flex-wrap items-center gap-0.5 px-2 py-1.5 border-b border-[var(--color-border)] bg-[var(--color-surface)] shrink-0">
          {editorMode !== 'preview' && toolbarActions.map((item, i) =>
            'type' in item && item.type === 'divider' ? (
              <div key={i} className="w-px h-5 bg-[var(--color-border)] mx-1" />
            ) : (
              <button
                key={i}
                type="button"
                onClick={'action' in item ? item.action : undefined}
                title={'title' in item ? item.title : ''}
                className="p-1.5 rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)] transition-all duration-200"
              >
                {'icon' in item && item.icon}
              </button>
            )
          )}
          {/* 模式切换 + 标签 */}
          <div className="ml-auto flex items-center gap-1">
            {renderAiWriteButtons()}
            <div className="flex items-center rounded-lg border border-[var(--color-border)] overflow-hidden">
              <button
                type="button"
                onClick={() => setEditorMode('edit')}
                title="纯编辑"
                className={`p-1.5 transition-all duration-200 ${
                  editorMode === 'edit'
                    ? 'bg-[var(--color-accent)] text-[var(--color-on-accent)]'
                    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
                }`}
              >
                <Pencil size={14} />
              </button>
              <button
                type="button"
                onClick={() => setEditorMode('split')}
                title="分栏模式"
                className={`p-1.5 transition-all duration-200 ${
                  editorMode === 'split'
                    ? 'bg-[#1677ff] text-white'
                    : 'text-[#5a6a8a] hover:bg-[#e8f0fe] hover:text-[#1677ff]'
                }`}
              >
                <Columns size={14} />
              </button>
              <button
                type="button"
                onClick={() => setEditorMode('preview')}
                title="纯预览"
                className={`p-1.5 transition-all duration-200 ${
                  editorMode === 'preview'
                    ? 'bg-[#1677ff] text-white'
                    : 'text-[#5a6a8a] hover:bg-[#e8f0fe] hover:text-[#1677ff]'
                }`}
              >
                <Eye size={14} />
              </button>
            </div>
            <span className="text-xs text-[var(--color-text-tertiary)] select-none ml-1">{t('note.format.md')}</span>
          </div>
        </div>
        )}

        {/* 内容区域（flex-1 填充，替代原 calc(100vh-380px) 视口高度） */}
        {isTxt ? (
          /* 纯文本模式：单栏 textarea，无预览、无语法工具栏 */
          <GhostTextArea
            textareaRef={textareaRef}
            value={content}
            onChange={handleContentChange}
            onKeyDown={handleEditorKeyDown}
            onInteract={autocomplete.dismiss}
            completion={autocomplete.completion}
            isLoading={autocomplete.isLoading}
            ghostAt={autocomplete.anchor}
            enabled={false}
            mono={false}
            placeholder={t('note.content')}
            className="flex-1 w-full min-h-0"
          />
        ) : (
        <div className="flex flex-1 min-h-0">
          {/* 左侧：Markdown 源码编辑 */}
          {editorMode !== 'preview' && (
            <div className={`flex flex-col transition-all duration-300 ${
              editorMode === 'edit' ? 'w-full' : 'w-1/2 border-r border-[var(--color-border)]'
            }`}>
              {editorMode === 'edit' && (
                <div className="px-3 py-1.5 text-xs text-[var(--color-text-tertiary)] border-b border-[var(--color-border)] bg-[var(--color-surface-alt)] select-none shrink-0">
                  编辑
                </div>
              )}
              <GhostTextArea
                textareaRef={textareaRef}
                value={content}
                onChange={handleContentChange}
                onKeyDown={handleEditorKeyDown}
                onInteract={autocomplete.dismiss}
                completion={autocomplete.completion}
                isLoading={autocomplete.isLoading}
                ghostAt={autocomplete.anchor}
                enabled={!isMobile}
                mono
                placeholder={t('note.content')}
                className="flex-1 w-full min-h-0"
              />
            </div>
          )}

          {/* 右侧：实时渲染预览 */}
          {editorMode !== 'edit' && (
            <div className={`flex flex-col transition-all duration-300 ${
              editorMode === 'preview' ? 'w-full' : 'w-1/2'
            }`}>
              {editorMode === 'preview' && (
                <div className="px-3 py-1.5 text-xs text-[var(--color-text-tertiary)] border-b border-[var(--color-border)] bg-[var(--color-surface-alt)] select-none shrink-0">
                  预览
                </div>
              )}
              <div className="flex-1 overflow-y-auto p-4 min-h-0">
                {content.trim() ? (
                  <div className="md-prose">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      rehypePlugins={[rehypeHighlight]}
                    >
                      {/* 转义行内角括号：避免泛型 <T> 被当作原始 HTML 丢弃 */}
                      {escapeInlineHtml(content)}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-[var(--color-text-tertiary)] text-sm italic">预览区域</p>
                )}
              </div>
            </div>
          )}
        </div>
        )}

        {renderAiResultBar()}
      </div>
    </div>
  );
}
