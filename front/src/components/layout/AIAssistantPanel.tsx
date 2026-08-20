/**
 * AI 对话浮层（v2 唯一 AI 入口，阶段二：全功能版）
 *
 * 承载原 AIChat 页全部功能（改造红线逐项保留）：
 * - SSE 流式输出 + 停止按钮（useChatComposer → useSSE → endpoints.chat.query）
 * - 深度思考 / 附件上传（图≤6 视频≤1）/ 引用笔记 / PPT 模板
 * - Plan 进度 + Reflection 反思状态（MessageBubble 内渲染）
 * - PptDownloadCard / TtsAudioCard（tool_file 事件，MessageBubble 内渲染）
 * - 历史会话视图：列表 / 切换 / 内联重命名 / 内联确认删除
 *
 * 视觉规格：宽 480px（移动端 w-full），fixed top-14 right-0 bottom-0，
 * z-index 90，滑入/滑出 300ms ease-in-out。
 *
 * 实现注意：
 * - 浮层有 transform（translate-x-*），内部 position:fixed 元素会相对浮层定位
 *   （containing block），故删除确认不用 ConfirmDialog（fixed 全屏遮罩会错位），
 *   改用内联二次确认；NotePicker / TemplatePicker 为 absolute 锚定布局，无此问题。
 * - 浮层常驻挂载（仅平移隐藏），流式状态跨开合保留；Escape/关闭只隐藏不打断。
 */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  History, Plus, X, Search, FileText, HelpCircle, Send, Square,
  Brain, Paperclip, Presentation, Trash2, Pencil, RefreshCw, MessageSquare, Check,
} from 'lucide-react';
import { useChatComposer } from '../../hooks/useChatComposer';
import { useUserStore } from '../../stores/useUserStore';
import type { ChatSession } from '../../types/api';
import MessageBubble from '../chat/MessageBubble';
import AttachmentBar from '../chat/AttachmentBar';
import NotePicker from '../chat/NotePicker';
import TemplatePicker from '../chat/TemplatePicker';

/** 浮层视图：对话 / 历史会话 */
export type AssistantView = 'chat' | 'history';

interface AIAssistantPanelProps {
  isVisible: boolean;
  view: AssistantView;
  onViewChange: (view: AssistantView) => void;
  onClose: () => void;
}

/** 快捷操作配置：点击将预设提示词填入输入框（v1 的自动检索/总结为后续增强） */
const quickActions = [
  { key: 'find', icon: Search, labelKey: 'assistant.quickFind', prompt: '帮我找一下关于 XXX 的笔记' },
  { key: 'summary', icon: FileText, labelKey: 'assistant.quickSummary', prompt: '请总结提炼这篇笔记的要点' },
  { key: 'answer', icon: HelpCircle, labelKey: 'assistant.quickAnswer', prompt: '请基于我的笔记回答：' },
];

/** 空状态功能介绍（纵向紧凑列表） */
const featureCards = [
  { key: 'find', icon: Search, titleKey: 'assistant.featureFindTitle', descKey: 'assistant.featureFindDesc' },
  { key: 'summary', icon: FileText, titleKey: 'assistant.featureSummaryTitle', descKey: 'assistant.featureSummaryDesc' },
  { key: 'answer', icon: HelpCircle, titleKey: 'assistant.featureAnswerTitle', descKey: 'assistant.featureAnswerDesc' },
];

/** 快捷问题（与旧 AIChat 一致） */
const quickQuestions = ['Deepseek大模型最新进展', 'AI Agent领域最新进展', '帮我搜索ChatGPT的最新论文并整理成笔记'];

/** 时间格式化（会话列表用） */
function formatTime(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString();
}

// ==================== 历史会话视图 ====================

interface SessionsViewProps {
  sessions: ChatSession[];
  currentSessionId: string | null;
  loading: boolean;
  onRefresh: () => void;
  onOpen: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}

/** 历史会话视图：列表 + 切换 / 内联重命名 / 内联二次确认删除 */
function SessionsView({ sessions, currentSessionId, loading, onRefresh, onOpen, onRename, onDelete }: SessionsViewProps) {
  const { t } = useTranslation();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [confirmId, setConfirmId] = useState<string | null>(null);

  /** 提交重命名 */
  const submitRename = () => {
    if (editingId && draftTitle.trim()) {
      onRename(editingId, draftTitle.trim());
    }
    setEditingId(null);
  };

  return (
    <div className="flex flex-col h-full">
      {/* 视图头部：标题 + 刷新 */}
      <div className="flex items-center justify-between px-4 h-12 border-b border-[var(--color-assistant-border)] shrink-0">
        <h3 className="text-sm font-semibold text-[var(--color-text)]">{t('assistant.viewHistory')}</h3>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="p-1.5 rounded-lg text-[var(--color-text-tertiary)]
            hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-text)]
            transition-colors duration-150"
          title={t('assistant.refresh')}
        >
          <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto">
        {loading && sessions.length === 0 ? (
          <p className="text-center text-[var(--color-text-tertiary)] text-sm py-10">{t('common.loading')}</p>
        ) : sessions.length === 0 ? (
          <div className="flex flex-col items-center py-16 text-[var(--color-text-tertiary)]">
            <MessageSquare size={36} className="mb-3 opacity-50" />
            <p className="text-xs">{t('assistant.historyEmpty')}</p>
          </div>
        ) : (
          sessions.map((session) => {
            const isCurrent = session.id === currentSessionId;
            return (
              <div
                key={session.id}
                className={`group flex items-center gap-2.5 px-4 py-3 border-b border-[var(--color-assistant-border)]
                  cursor-pointer transition-colors duration-150 ${
                  isCurrent
                    ? 'bg-[var(--color-accent-bg)] border-l-2 border-l-[var(--color-accent)]'
                    : 'hover:bg-[var(--color-surface-alt)]'
                }`}
                onClick={() => {
                  if (confirmId === session.id) return;
                  onOpen(session.id);
                }}
              >
                <MessageSquare size={15} className="text-[var(--color-text-tertiary)] shrink-0" />

                {/* 标题 / 内联重命名输入框 */}
                {editingId === session.id ? (
                  <input
                    type="text"
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') submitRename();
                      if (e.key === 'Escape') setEditingId(null);
                    }}
                    onClick={(e) => e.stopPropagation()}
                    autoFocus
                    className="flex-1 min-w-0 px-2 py-1 text-sm rounded
                      bg-[var(--color-card)] border border-[var(--color-accent)]
                      text-[var(--color-text)] focus:outline-none"
                  />
                ) : (
                  <div className="flex-1 min-w-0">
                    <h4 className="text-sm font-medium text-[var(--color-text)] truncate">{session.title}</h4>
                    <p className="text-[10px] text-[var(--color-text-tertiary)]">
                      {formatTime(session.updated_at)}
                      {session.message_count != null && ` · ${session.message_count} ${t('assistant.messageCount')}`}
                    </p>
                  </div>
                )}

                {/* 操作按钮：重命名 / 删除（内联二次确认） */}
                {editingId === session.id ? (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      submitRename();
                    }}
                    className="p-1.5 text-[var(--color-accent)]"
                    title={t('common.confirm')}
                  >
                    <Check size={15} />
                  </button>
                ) : confirmId === session.id ? (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(session.id);
                      setConfirmId(null);
                    }}
                    className="px-2 py-1 text-xs font-medium rounded-md
                      bg-[var(--color-danger)] text-white hover:opacity-90 shrink-0"
                  >
                    {t('assistant.deleteConfirm')}
                  </button>
                ) : (
                  <div className="flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingId(session.id);
                        setDraftTitle(session.title);
                      }}
                      className="p-1.5 rounded text-[var(--color-text-tertiary)]
                        hover:text-[var(--color-accent)] transition-colors"
                      title={t('assistant.rename')}
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmId(session.id);
                      }}
                      className="p-1.5 rounded text-[var(--color-text-tertiary)]
                        hover:text-[var(--color-danger)] transition-colors"
                      title={t('assistant.delete')}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ==================== 浮层主体 ====================

export default function AIAssistantPanel({ isVisible, view, onViewChange, onClose }: AIAssistantPanelProps) {
  const { t } = useTranslation();
  const chat = useChatComposer();
  const userAvatar = useUserStore((s) => s.userInfo?.avatar);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // 打开浮层时刷新会话列表（保证历史视图展示最新会话）
  // refreshSessions 为 useCallback 稳定引用
  const refreshSessions = chat.refreshSessions;
  useEffect(() => {
    if (isVisible) {
      refreshSessions();
    }
  }, [isVisible, refreshSessions]);

  // 自动滚动到底部（仅对话视图）
  useEffect(() => {
    if (view !== 'chat') return;
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chat.messages, view]);

  // 发送按钮禁用态（与旧 AIChat 一致）
  const sendDisabled =
    (!chat.input.trim() &&
      chat.pendingAttachments.filter((a) => a.status === 'done' && a.fileId).length === 0) ||
    chat.pendingAttachments.some((a) => a.status === 'uploading');

  return (
    <aside
      className={`fixed top-14 right-0 bottom-0 w-[480px] max-w-full border-l border-[var(--color-assistant-border)]
        transition-transform duration-300 ease-in-out flex flex-col
        ${isVisible ? 'translate-x-0' : 'translate-x-full'}`}
      style={{
        zIndex: 90,
        background: 'var(--color-assistant-bg)',
        boxShadow: 'var(--color-assistant-shadow)',
      }}
      aria-hidden={!isVisible}
    >
      {/* 头部：角色问候 + 快捷操作 + 视图切换 / 新建 / 关闭 */}
      <div
        className="px-4 py-3 border-b border-[var(--color-assistant-border)] shrink-0"
        style={{ background: 'radial-gradient(ellipse 120% 100% at 50% 0%, rgba(74,158,255,0.07) 0%, transparent 70%)' }}
      >
        <div className="flex items-center gap-3 mb-2.5">
          {/* 小云云角色头像 */}
          <div className="w-10 h-10 rounded-full overflow-hidden shrink-0">
            <img src="/avatars/xiaoyunyun.png" alt="小云云" className="w-full h-full object-cover" />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="text-base font-bold text-[var(--color-text)]">{t('assistant.title')}</h3>
            <p className="text-xs text-[var(--color-text-secondary)] truncate">{t('assistant.subtitle')}</p>
          </div>

          {/* 历史会话视图切换 */}
          <button
            onClick={() => onViewChange(view === 'chat' ? 'history' : 'chat')}
            className={`p-1.5 rounded-lg transition-colors duration-150 ${
              view === 'history'
                ? 'text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-accent)]'
            }`}
            title={t('assistant.viewHistory')}
          >
            <History size={18} />
          </button>
          {/* 新对话 */}
          <button
            onClick={() => {
              chat.handleNewChat();
              onViewChange('chat');
            }}
            className="p-1.5 rounded-lg text-[var(--color-text-secondary)]
              hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-accent)]
              transition-colors duration-150"
            title={t('assistant.newChat')}
          >
            <Plus size={18} />
          </button>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-[var(--color-text-secondary)]
              hover:bg-[var(--color-surface-alt)] hover:text-[var(--color-text)]
              transition-colors duration-150"
            title={t('assistant.close')}
          >
            <X size={18} />
          </button>
        </div>

        {/* 快捷操作按钮（对话视图时显示；点击将预设提示词填入输入框） */}
        {view === 'chat' && (
          <div className="flex gap-2">
            {quickActions.map((action) => {
              const Icon = action.icon;
              return (
                <button
                  key={action.key}
                  onClick={() => chat.setInput(action.prompt)}
                  className="flex items-center gap-1 px-3 py-1.5 rounded-full text-xs font-medium
                    bg-[var(--color-quick-action-bg)] text-[var(--color-quick-action-text)]
                    border border-[var(--color-quick-action-border)]
                    hover:opacity-90 transition-opacity duration-200"
                >
                  <Icon size={14} />
                  {t(action.labelKey)}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {view === 'history' ? (
        /* ===== 历史会话视图 ===== */
        <SessionsView
          sessions={chat.sessions}
          currentSessionId={chat.currentSessionId}
          loading={chat.sessionsLoading}
          onRefresh={chat.refreshSessions}
          onOpen={(id) => {
            chat.loadSession(id);
            onViewChange('chat');
          }}
          onRename={chat.renameSession}
          onDelete={chat.deleteSession}
        />
      ) : (
        <>
          {/* ===== 对话视图：消息流 ===== */}
          <div className="flex-1 overflow-y-auto px-4 py-3">
            {chat.loadingMessages ? (
              <div className="text-center py-16">
                <p className="text-[var(--color-text-tertiary)]">{t('common.loading')}</p>
              </div>
            ) : chat.messages.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full max-w-sm mx-auto">
                {/* 欢迎区域 */}
                <div className="text-center mb-6">
                  <div className="relative mx-auto mb-4 w-20 h-20">
                    {/* 头像光晕背景 */}
                    <div className="absolute inset-0 rounded-full bg-gradient-to-br from-[#4a9eff]/20 to-[#4a9eff]/5 blur-xl" />
                    <div className="relative w-20 h-20 rounded-full overflow-hidden ring-2 ring-[#4a9eff]/20 ring-offset-2 ring-offset-[var(--color-assistant-bg)]">
                      <img src="/avatars/xiaoyunyun.png" alt="小云云" className="w-full h-full object-cover" />
                    </div>
                  </div>
                  <h2 className="text-xl font-bold mb-1.5 bg-gradient-to-r from-[#1a2a4a] to-[#4a9eff] bg-clip-text text-transparent">{t('assistant.emptyTitle')}</h2>
                  <p className="text-xs text-[var(--color-text-secondary)]">{t('assistant.emptyDesc')}</p>
                </div>

                {/* 快捷问题 */}
                <div className="w-full mb-5">
                  <p className="text-xs font-medium text-[var(--color-text-tertiary)] mb-2.5 text-center">
                    {t('assistant.quickQuestionsLabel')}
                  </p>
                  <div className="flex flex-wrap justify-center gap-2">
                    {quickQuestions.map((q) => (
                      <button
                        key={q}
                        onClick={() => chat.setInput(q)}
                        className="px-3 py-1.5 rounded-full text-xs
                          bg-[var(--color-card)] border border-[var(--color-border)]
                          text-[var(--color-text-secondary)] hover:border-[var(--color-accent)]
                          hover:text-[var(--color-accent)] transition-colors duration-200"
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>

                {/* 功能介绍（纵向紧凑列表） */}
                <div className="w-full space-y-2">
                  {featureCards.map((f) => {
                    const Icon = f.icon;
                    return (
                      <div
                        key={f.key}
                        className="flex items-center gap-3 p-3 rounded-xl bg-[var(--color-card)] border border-[var(--color-border)] transition-all duration-200 hover:shadow-[var(--shadow-accent-md)] hover:border-[var(--color-assistant-ai-bubble-border)] hover:-translate-y-0.5 cursor-default"
                      >
                        <Icon size={18} className="text-[var(--color-accent)] shrink-0" />
                        <div className="min-w-0">
                          <h4 className="text-xs font-medium text-[var(--color-text)]">{t(f.titleKey)}</h4>
                          <p className="text-[11px] text-[var(--color-text-tertiary)]">{t(f.descKey)}</p>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                {chat.messages.map((msg, idx) => (
                  <MessageBubble
                    key={msg.id}
                    msg={msg}
                    isLast={idx === chat.messages.length - 1}
                    isStreaming={chat.isStreaming}
                    userAvatar={userAvatar}
                    plan={chat.planState}
                    thinkingText={chat.thinkingText}
                  />
                ))}
                <div ref={messagesEndRef} />
              </div>
            )}
          </div>

          {/* ===== 对话视图：输入区 ===== */}
          <div className="px-4 pb-3 border-t border-[var(--color-assistant-border)] shrink-0">
            {/* 工具按钮行：附件 / 深度思考 / 引用笔记 / PPT 模板（图标 + 文字，Tooltip 同 title） */}
            <div className="flex items-center gap-1.5 pt-2.5 pb-1.5">
              {/* 附件上传按钮 */}
              <button
                onClick={() => chat.fileInputRef.current?.click()}
                disabled={chat.isStreaming}
                className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
                  chat.pendingAttachments.length > 0
                    ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
                } disabled:opacity-50`}
                title={t('chat.attachmentUpload')}
              >
                <Paperclip size={14} />
                {t('chat.attachment')}
              </button>
              {/* 隐藏的文件选择器（图片/视频，可多选） */}
              <input
                ref={chat.fileInputRef}
                type="file"
                accept="image/*,video/*"
                multiple
                hidden
                onChange={chat.handleFileSelect}
              />

              {/* 深度思考开关 */}
              <button
                onClick={() => chat.setEnableThinking(!chat.enableThinking)}
                className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
                  chat.enableThinking
                    ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
                }`}
                title={t('chat.thinkingMode')}
              >
                <Brain size={14} />
                {t('chat.thinkingMode')}
              </button>

              {/* 引用笔记按钮 */}
              <button
                onClick={() => chat.setShowNotePicker((v) => !v)}
                className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
                  chat.showNotePicker || chat.selectedNotes.length > 0
                    ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
                }`}
                title={t('chat.noteRef')}
              >
                <FileText size={14} />
                {t('chat.noteRef')}
              </button>

              {/* PPT 模板按钮 */}
              <button
                onClick={() => chat.setShowTemplatePicker((v) => !v)}
                className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
                  chat.showTemplatePicker || chat.selectedTemplate
                    ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
                }`}
                title={t('chat.pptTemplate')}
              >
                <Presentation size={14} />
                {t('chat.pptTemplate')}
              </button>

              {/* 选择面板（absolute 锚定布局，浮层内正常展示） */}
              <div className="relative">
                <TemplatePicker
                  open={chat.showTemplatePicker}
                  onClose={() => chat.setShowTemplatePicker(false)}
                  selected={chat.selectedTemplate}
                  onSelect={chat.toggleTemplate}
                />
                <NotePicker
                  open={chat.showNotePicker}
                  onClose={() => chat.setShowNotePicker(false)}
                  selectedIds={chat.selectedNoteIds}
                  onToggle={chat.toggleNote}
                />
              </div>
            </div>

            {/* 输入行：输入框 + 发送/停止 */}
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={chat.input}
                onChange={(e) => chat.setInput(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && chat.handleSend()}
                placeholder={t('chat.placeholder')}
                className="flex-1 h-10 px-3 rounded-lg text-sm
                  bg-[var(--color-search-bg)] border border-[var(--color-search-border)]
                  text-[var(--color-text)] placeholder-[var(--color-text-tertiary)]
                  focus:outline-none focus:border-[var(--color-accent)]
                  transition-colors duration-200"
                disabled={chat.isStreaming}
              />
              {chat.isStreaming ? (
                <button
                  onClick={chat.stopStreaming}
                  className="w-10 h-10 rounded-lg bg-[var(--color-danger)] text-white
                    flex items-center justify-center hover:opacity-90 shrink-0"
                  title={t('chat.stop')}
                >
                  <Square size={16} />
                </button>
              ) : (
                <button
                  onClick={chat.handleSend}
                  disabled={sendDisabled}
                  className="w-10 h-10 rounded-lg bg-[var(--color-accent)] text-white
                    flex items-center justify-center hover:bg-[var(--color-accent-hover)]
                    disabled:opacity-50 transition-colors duration-200 shrink-0"
                  title={t('chat.send')}
                >
                  <Send size={18} />
                </button>
              )}
            </div>

            {/* 附件预览条（发送后清空） */}
            <AttachmentBar items={chat.pendingAttachments} onRemove={chat.removeAttachment} />

            {/* 引用笔记标签：手动选择优先；无手动选择时展示自动关联的当前笔记（v2） */}
            {chat.effectiveNotes.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-2">
                {chat.effectiveNotes.map((note) => {
                  const isAuto = chat.autoNote?.id === note.id;
                  return (
                    <span
                      key={note.id}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-[var(--radius-md)]
                        bg-[var(--color-accent-bg)] text-[var(--color-accent)] border border-[var(--color-accent)]/20"
                    >
                      <FileText size={12} />
                      <span className="max-w-[120px] truncate">{note.title}</span>
                      {/* 自动关联标记：清晰展示当前对话基于哪篇笔记执行 */}
                      {isAuto && (
                        <span className="text-[10px] opacity-70 shrink-0">{t('assistant.autoTag')}</span>
                      )}
                      <button
                        onClick={() => (isAuto ? chat.dismissAutoNote() : chat.removeNote(note.id))}
                        className="hover:opacity-70"
                      >
                        <X size={12} />
                      </button>
                    </span>
                  );
                })}
              </div>
            )}

            {/* PPT 模板标签 */}
            {chat.selectedTemplate && (
              <div className="flex flex-wrap gap-1.5 mt-2">
                <span className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-[var(--radius-md)]
                  bg-[var(--color-accent-bg)] text-[var(--color-accent)] border border-[var(--color-accent)]/20">
                  <Presentation size={12} />
                  <span className="max-w-[160px] truncate">{t('chat.templateTag')}{chat.selectedTemplate.name}</span>
                  <button onClick={() => chat.toggleTemplate(chat.selectedTemplate!)} className="hover:opacity-70">
                    <X size={12} />
                  </button>
                </span>
              </div>
            )}
          </div>
        </>
      )}
    </aside>
  );
}
