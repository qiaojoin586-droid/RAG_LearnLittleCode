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

  /** 向 textarea 插入/包裹选中文本 */
  const wrapSelection = useCallback((before: string, after: string = before) => {
    const ta = textareaRef.current;
    if (!ta) return;
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
  }, [content]);

  /** 在行首插入前缀 */
  const prefixLine = useCallback((prefix: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    // 找到当前行的起始位置
    const lineStart = content.lastIndexOf('\n', start - 1) + 1;
    const newContent = content.substring(0, lineStart) + prefix + content.substring(lineStart);
    setContent(newContent);
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(start + prefix.length, start + prefix.length);
    });
  }, [content]);

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
          <div className="flex items-center justify-end px-2 py-1.5 border-b border-[var(--color-border)] bg-[var(--color-surface)] shrink-0">
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
          <textarea
            ref={textareaRef}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder={t('note.content')}
            className="flex-1 w-full resize-none p-4 text-sm leading-relaxed text-[var(--color-text)] bg-transparent outline-none min-h-0"
            spellCheck={false}
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
              <textarea
                ref={textareaRef}
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder={t('note.content')}
                className="flex-1 w-full resize-none p-4 text-sm leading-relaxed text-[var(--color-text)] bg-transparent outline-none font-[var(--font-mono)] min-h-0"
                spellCheck={false}
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
      </div>
    </div>
  );
}
