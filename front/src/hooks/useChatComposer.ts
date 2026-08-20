/**
 * AI 对话编排 Hook（阶段二：从 AIChat.tsx 抽取，供浮层唯一化使用）
 *
 * 封装：发送 / SSE 生命周期 / 附件 / 引用笔记 / PPT 模板 /
 *      Plan 进度 / 反思状态 / 会话列表管理（刷新/切换/重命名/删除）。
 *
 * 会话语义（不动后端）：发送时读 useSessionStore.currentSessionId
 * （null = 新会话，后端自动创建并生成标题，chat.py:129/148）；
 * onDone 回调记录新会话 ID 并刷新会话列表。
 *
 * 状态复用：messages / sessions / currentSessionId 等沿用 useSessionStore，
 * 浮层是唯一消费方（AIChat 页已废弃），无需独立消息字段。
 */

import { useState, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useSSE } from './useSSE';
import { useSessionStore } from '../stores/useSessionStore';
import { useNoteContextStore } from '../stores/useNoteContextStore';
import { sessionsApi } from '../api/sessions';
import { endpoints } from '../api/endpoints';
import { uploadChatFile, deleteChatFile } from '../api/chat';
import type { ChatMessage, Note, AttachmentMeta } from '../types/api';
import type { PptTemplateInfo } from '../api/pptTemplates';
import type { PlanStepData } from '../components/chat/PlanProgressCard';
import type { PlanDisplayState } from '../components/chat/MessageBubble';
import type { PendingAttachment } from '../components/chat/AttachmentBar';

// 附件限制（与后端 .env 配置保持一致）
const MAX_IMAGES_PER_MSG = 6;
const MAX_VIDEOS_PER_MSG = 1;
const MAX_IMAGE_MB = 10;
const MAX_VIDEO_MB = 50;

export function useChatComposer() {
  const { start, stop } = useSSE();
  const { t } = useTranslation();

  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  // 思考阶段日志仍在 SSE 回调中收集（保留收集便于将来恢复展示）
  const [, setThinkingStages] = useState<{ stage: string; content: string }[]>([]);
  // 深度思考模式的实时思考内容（thinking 事件）
  const [thinkingText, setThinkingText] = useState('');
  // 深度思考开关（默认关闭；开启后请求带 enable_thinking=true，后端切换思考模型）
  const [enableThinking, setEnableThinking] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  // 引用笔记状态
  const [selectedNotes, setSelectedNotes] = useState<Note[]>([]);
  const [showNotePicker, setShowNotePicker] = useState(false);

  // 自动关联（v2）：当前详情区打开的笔记（useNoteContextStore，由 NoteList 写入）
  const currentNote = useNoteContextStore((s) => s.currentNote);
  // 手动解除的自动关联笔记 ID（切换笔记后自动恢复关联）
  const [dismissedAutoNoteId, setDismissedAutoNoteId] = useState<string | null>(null);

  // 自动关联笔记：仅在未手动选择任何笔记、且未被手动解除时生效（需求边界：手动选择优先）
  const autoNote =
    currentNote && selectedNotes.length === 0 && currentNote.id !== dismissedAutoNoteId
      ? currentNote
      : null;

  // 实际参与发送的引用笔记 = 手动选择（优先）∪ 自动关联（兜底）
  const effectiveNotes = selectedNotes.length > 0 ? selectedNotes : autoNote ? [autoNote] : [];

  /** 手动解除当前自动关联（切换笔记后自动恢复） */
  const dismissAutoNote = () => {
    if (currentNote) setDismissedAutoNoteId(currentNote.id);
  };

  // PPT 模板选择状态（单选）
  const [selectedTemplate, setSelectedTemplate] = useState<PptTemplateInfo | null>(null);
  const [showTemplatePicker, setShowTemplatePicker] = useState(false);

  // 附件状态（上传预览条）
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Plan-and-Execute 进度状态
  const [planGoal, setPlanGoal] = useState('');
  const [planSteps, setPlanSteps] = useState<PlanStepData[]>([]);
  const [planTotalSteps, setPlanTotalSteps] = useState(0);
  const [planCompletedSteps, setPlanCompletedSteps] = useState(0);
  const [planActive, setPlanActive] = useState(false);
  const [planComplete, setPlanComplete] = useState(false);
  const [currentTool, setCurrentTool] = useState('');
  // 反思状态（L1 综合后自检 + L2 失败修复：checking=反思 / refining=修正 / repairing=修复重试）
  const [planReflectStage, setPlanReflectStage] = useState<'' | 'checking' | 'refining' | 'repairing'>('');
  const [planReflectRound, setPlanReflectRound] = useState(0);

  // requestAnimationFrame 节流：合并高频 token 更新
  const rafRef = useRef<number | null>(null);
  const pendingContentRef = useRef('');

  const messages = useSessionStore((s) => s.messages);
  const sessions = useSessionStore((s) => s.sessions);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const addMessage = useSessionStore((s) => s.addMessage);
  const setMessages = useSessionStore((s) => s.setMessages);
  const setSessions = useSessionStore((s) => s.setSessions);
  const setCurrentSession = useSessionStore((s) => s.setCurrentSession);
  const setLastSessionId = useSessionStore((s) => s.setLastSessionId);
  const updateLastAssistantMessage = useSessionStore((s) => s.updateLastAssistantMessage);
  const clearCurrentSession = useSessionStore((s) => s.clearCurrentSession);

  /** 刷新会话列表（历史视图展示 / 新会话创建后调用） */
  const refreshSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const res = await sessionsApi.list();
      setSessions(res.data?.data?.sessions ?? []);
    } catch (err) {
      console.error('[AIAssistantPanel] 加载会话列表失败:', err);
    } finally {
      setSessionsLoading(false);
    }
  }, [setSessions]);

  /** 切换笔记选中状态 */
  const toggleNote = (note: Note) => {
    setSelectedNotes((prev) => {
      const exists = prev.find((n) => n.id === note.id);
      if (exists) return prev.filter((n) => n.id !== note.id);
      return [...prev, note];
    });
  };

  /** 切换模板选中状态（单选；再次点击取消） */
  const toggleTemplate = (tmpl: PptTemplateInfo) => {
    setSelectedTemplate((prev) => (prev?.id === tmpl.id ? null : tmpl));
  };

  /** 移除已引用的笔记 */
  const removeNote = (noteId: string) => {
    setSelectedNotes((prev) => prev.filter((n) => n.id !== noteId));
  };

  /** 选择文件（前端预校验：类型/大小/数量上限，然后逐个上传） */
  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = ''; // 允许重复选择同一文件

    if (files.length === 0) return;

    let imgCount = pendingAttachments.filter((a) => a.fileType === 'image').length;
    let vidCount = pendingAttachments.filter((a) => a.fileType === 'video').length;

    const newItems: PendingAttachment[] = [];
    for (const file of files) {
      const isImage = file.type.startsWith('image/');
      const isVideo = file.type.startsWith('video/');
      if (!isImage && !isVideo) {
        alert(`不支持的文件类型: ${file.name}（仅支持图片/视频）`);
        continue;
      }
      const maxMB = isImage ? MAX_IMAGE_MB : MAX_VIDEO_MB;
      if (file.size > maxMB * 1024 * 1024) {
        alert(`${file.name} 超过大小限制 ${maxMB}MB`);
        continue;
      }
      if (isImage && imgCount >= MAX_IMAGES_PER_MSG) {
        alert(`单条消息最多上传 ${MAX_IMAGES_PER_MSG} 张图片`);
        break;
      }
      if (isVideo && vidCount >= MAX_VIDEOS_PER_MSG) {
        alert(`单条消息最多上传 ${MAX_VIDEOS_PER_MSG} 个视频`);
        continue;
      }

      const att: PendingAttachment = {
        localId: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        file,
        fileType: isImage ? 'image' : 'video',
        name: file.name,
        previewUrl: URL.createObjectURL(file), // 本地预览（objectURL）
        status: 'uploading',
        progress: 0,
      };
      newItems.push(att);
      if (isImage) imgCount++;
      else vidCount++;
    }

    if (newItems.length === 0) return;
    setPendingAttachments((prev) => [...prev, ...newItems]);
    newItems.forEach(uploadAttachment);
  };

  /** 上传单个附件（XHR 带进度，成功后记录 file_id） */
  const uploadAttachment = (att: PendingAttachment) => {
    uploadChatFile(att.file, (percent) => {
      setPendingAttachments((prev) =>
        prev.map((a) => (a.localId === att.localId ? { ...a, progress: percent } : a))
      );
    })
      .then((resp) => {
        setPendingAttachments((prev) =>
          prev.map((a) =>
            a.localId === att.localId
              ? {
                  ...a,
                  status: 'done',
                  fileId: resp.file_id,
                  width: resp.width ?? undefined,
                  height: resp.height ?? undefined,
                }
              : a
          )
        );
      })
      .catch((err) => {
        console.error('[AIAssistantPanel] 附件上传失败:', err);
        setPendingAttachments((prev) =>
          prev.map((a) => (a.localId === att.localId ? { ...a, status: 'error' } : a))
        );
      });
  };

  /** 移除附件（已上传的调 DELETE 接口；未发送可删） */
  const removeAttachment = (localId: string) => {
    const target = pendingAttachments.find((a) => a.localId === localId);
    if (!target) return;
    if (target.previewUrl) URL.revokeObjectURL(target.previewUrl);
    // 已上传且未随消息发送 → 调删除接口（失败由后端孤儿清理兜底）
    if (target.fileId) {
      deleteChatFile(target.fileId).catch((err) =>
        console.warn('[AIAssistantPanel] 附件删除失败:', err)
      );
    }
    setPendingAttachments((prev) => prev.filter((a) => a.localId !== localId));
  };

  /** 发送消息 */
  const handleSend = async () => {
    // 附件前置校验：正在上传 / 有失败附件
    const uploading = pendingAttachments.some((a) => a.status === 'uploading');
    const failed = pendingAttachments.some((a) => a.status === 'error');
    const readyAttachments = pendingAttachments.filter((a) => a.status === 'done' && a.fileId);
    const attachmentIds = readyAttachments.map((a) => a.fileId!);
    if ((!input.trim() && attachmentIds.length === 0) || isStreaming) return;
    if (uploading) {
      alert('附件正在上传，请稍候再发送');
      return;
    }
    if (failed) {
      alert('有附件上传失败，请移除后重试');
      return;
    }

    // 会话 ID：当前会话（null = 新会话，后端自动创建）
    const sessionId = currentSessionId;

    // 构建发送内容：若有引用笔记（手动选择优先，自动关联兜底），
    // 以「卡片」结构化形式发送给 LLM（每篇一张卡片：编号 + 标题 + 正文分块）；
    // <referenced_notes> 元数据块保持原格式（后端解析依赖，勿动）
    let messageText = input;
    if (effectiveNotes.length > 0) {
      const noteCards = effectiveNotes
        .map((n, i) => `【卡片 ${i + 1}｜${n.title}】\n${n.content}`)
        .join('\n\n');
      // 结构化引用块：包含笔记 ID，供后端解析并注入 Agent 上下文
      const noteRefBlock = effectiveNotes
        .map((n) => `- ID: ${n.id} | 标题: ${n.title}`)
        .join('\n');
      messageText = `${input}\n\n---\n以下是用户引用的笔记卡片：\n\n${noteCards}\n\n<referenced_notes>\n${noteRefBlock}\n</referenced_notes>`;
    }
    // 附加用户选择的 PPT 模板（后端解析注入 system_prompt，LLM 填入工具参数）
    if (selectedTemplate) {
      messageText += `\n\n<ppt_template>\n- ID: ${selectedTemplate.id} | 名称: ${selectedTemplate.name}\n</ppt_template>`;
      // 发送后清除模板选择（模板是一次性使用）
      setSelectedTemplate(null);
    }

    // 乐观用户消息：附带附件元数据（气泡立即渲染，历史回显走后端附件数据）
    const userAttachments: AttachmentMeta[] = readyAttachments.map((a) => ({
      file_id: a.fileId!,
      file_type: a.fileType,
      original_name: a.name,
      file_size: a.file.size,
      width: a.width,
      height: a.height,
    }));
    const userMsg: ChatMessage = {
      id: Date.now(),
      session_id: sessionId || '',
      role: 'user',
      // 显示完整组合文本（含引用块），与后端入库内容一致；
      // MessageContent 展示层将其解析为「问题 + 引用笔记卡片」（v2 卡片化）
      content: messageText,
      created_at: new Date().toISOString(),
      attachments: userAttachments.length > 0 ? userAttachments : undefined,
    };
    addMessage(userMsg);
    setInput('');
    setSelectedNotes([]);
    setIsStreaming(true);
    setThinkingStages([]);
    setThinkingText('');
    // 重置 Plan 状态
    setPlanGoal('');
    setPlanSteps([]);
    setPlanTotalSteps(0);
    setPlanCompletedSteps(0);
    setPlanActive(false);
    setPlanComplete(false);

    // 添加 AI 占位消息
    const aiMsg: ChatMessage = {
      id: Date.now() + 1,
      session_id: sessionId || '',
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
    };
    addMessage(aiMsg);

    let accumulated = '';

    // 节流刷新函数：用 rAF 合并高频 token 更新
    const scheduleFlush = () => {
      if (rafRef.current !== null) return; // 已有待处理的 rAF
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        if (pendingContentRef.current) {
          updateLastAssistantMessage(pendingContentRef.current);
          pendingContentRef.current = '';
        }
      });
    };

    /** 发送前刷新残留内容（done/error 与 finally 共用） */
    const flushPending = () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      if (pendingContentRef.current) {
        updateLastAssistantMessage(pendingContentRef.current);
        pendingContentRef.current = '';
      }
      setThinkingText('');
    };

    try {
      await start(
        endpoints.chat.query,
        {
          session_id: sessionId ?? undefined,
          message: messageText,
          enable_thinking: enableThinking,
          attachment_ids: attachmentIds,
        },
        {
          onThinking: (stage, content) => {
            setThinkingStages((prev) => [...prev, { stage, content }]);
            // 深度思考模式：实时更新思考内容供气泡展示（stage=thinking 为 reasoning_content）
            if (stage === 'thinking') setThinkingText(content);
          },
          onResponse: (content) => {
            accumulated += content;
            pendingContentRef.current = accumulated;
            scheduleFlush();
            // 回答开始流出，反思状态使命结束
            setPlanReflectStage('');
            setPlanReflectRound(0);
          },
          onDone: (newSessionId) => {
            flushPending();
            setIsStreaming(false);
            setCurrentTool('');
            // 新会话创建后记录 ID 并刷新历史会话列表（浮层无需改 URL）
            if (newSessionId) {
              setLastSessionId(newSessionId);
              setCurrentSession(newSessionId);
              refreshSessions();
            }
          },
          onError: (msg) => {
            // 确保错误前内容被刷新
            flushPending();
            updateLastAssistantMessage(`⚠️ ${msg}`);
            setIsStreaming(false);
            setCurrentTool('');
            setPlanReflectStage('');
            setPlanReflectRound(0);
          },
          // Plan-and-Execute 事件回调
          onPlanStart: (goal, totalSteps) => {
            setPlanActive(true);
            setPlanGoal(goal);
            setPlanTotalSteps(totalSteps);
          },
          onPlanStep: (step, action, status) => {
            setPlanSteps((prev) => {
              const existing = prev.find((s) => s.step === step);
              if (existing) {
                return prev.map((s) => s.step === step ? { ...s, status: status as PlanStepData['status'] } : s);
              }
              return [...prev, { step, action, status: status as PlanStepData['status'] }];
            });
          },
          onPlanStepStart: (step) => {
            setPlanSteps((prev) =>
              prev.map((s) => s.step === step ? { ...s, status: 'running' as const } : s)
            );
          },
          onPlanStepEnd: (step, result) => {
            setPlanSteps((prev) =>
              prev.map((s) => s.step === step ? { ...s, status: 'completed' as const, result } : s)
            );
            setPlanCompletedSteps((c) => c + 1);
          },
          onPlanSynthesize: () => {
            // 综合阶段开始，所有步骤已完成
          },
          onReflection: (stage, round) => {
            // L1 综合后自检 / L2 失败修复状态（AI对话Reflection反思机制设计方案.md §3.8）
            setPlanReflectStage(stage as 'checking' | 'refining' | 'repairing');
            setPlanReflectRound(round);
          },
          onPlanComplete: (totalSteps, completedSteps) => {
            setPlanTotalSteps(totalSteps);
            setPlanCompletedSteps(completedSteps);
            setPlanComplete(true);
          },
          onPlanFallback: () => {
            // Plan 失败降级为 ReAct，隐藏进度条
            setPlanActive(false);
            setCurrentTool('');
            setPlanReflectStage('');
            setPlanReflectRound(0);
          },
          // 工具调用事件
          onToolStart: (name) => {
            setCurrentTool(name);
          },
          onToolEnd: () => {
            setCurrentTool('');
          },
          // 工具产出文件事件（PPT 生成完成 / TTS 语音生成）
          onToolFile: (info) => {
            // 挂到最后一条 assistant 消息的 attachments（与历史回放统一渲染源；
            // 后端保存时同样写入 attachments_json，切换页面后卡片可恢复）
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const last = prev[prev.length - 1];
              const isTts = info.name === 'text_to_speech';
              const fileMeta: AttachmentMeta = {
                file_id: info.file_id,
                file_type: isTts ? 'tts' : 'ppt',
                original_name: isTts ? '语音朗读' : info.title || '讲解PPT',
                download_url: isTts ? info.audio_url : info.download_url,
                title: info.title,
                slide_count: info.slide_count,
                duration_estimate: info.duration_estimate,
              };
              return [
                ...prev.slice(0, -1),
                { ...last, attachments: [...(last.attachments || []), fileMeta] },
              ];
            });
          },
        }
      );
    } finally {
      // 无论正常结束、报错还是用户点击停止（AbortError），都确保重置流式状态
      setIsStreaming(false);
      flushPending();
      // 清理附件预览条（附件已随消息发出，fileId 由后端管理）
      pendingAttachments.forEach((a) => {
        if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
      });
      setPendingAttachments([]);
    }
  };

  /** 新建对话：停止流式并清空当前会话状态 */
  const handleNewChat = () => {
    stop();
    clearCurrentSession();
    setInput('');
    setIsStreaming(false);
    setThinkingText('');
    setSelectedNotes([]);
    setSelectedTemplate(null);
    setPlanGoal('');
    setPlanSteps([]);
    setPlanTotalSteps(0);
    setPlanCompletedSteps(0);
    setPlanActive(false);
    setPlanComplete(false);
    setPlanReflectStage('');
    setPlanReflectRound(0);
  };

  /** 加载指定会话的历史消息（切换会话；流式中切换先停止） */
  const loadSession = useCallback(
    async (id: string) => {
      stop();
      setCurrentSession(id);
      setLastSessionId(id);
      setLoadingMessages(true);
      try {
        const res = await sessionsApi.getMessages(id);
        setMessages(res.data?.data?.messages ?? []);
      } catch (err) {
        // 404 表示会话已被删除，清除残留状态
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 404) {
          console.warn('[AIAssistantPanel] 会话不存在(404)，清除残留状态');
          clearCurrentSession();
          toast.error(t('assistant.sessionNotFound'));
        } else {
          console.error('[AIAssistantPanel] 加载历史消息失败:', err);
          toast.error(t('assistant.loadFailed'));
        }
      } finally {
        setLoadingMessages(false);
      }
    },
    [stop, setCurrentSession, setLastSessionId, setMessages, clearCurrentSession, t]
  );

  /** 重命名会话（内联编辑提交） */
  const renameSession = useCallback(
    async (id: string, title: string) => {
      try {
        await sessionsApi.updateTitle(id, { title });
        setSessions((prev) =>
          prev.map((s) => (s.id === id ? { ...s, title } : s))
        );
      } catch (err) {
        console.error('[AIAssistantPanel] 重命名会话失败:', err);
        toast.error(t('assistant.renameFailed'));
      }
    },
    [setSessions, t]
  );

  /** 删除会话（删除当前会话时清空对话状态） */
  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await sessionsApi.delete(id);
        setSessions((prev) => prev.filter((s) => s.id !== id));
        const { currentSessionId: cur, lastSessionId: last, clearCurrentSession: clearCur } =
          useSessionStore.getState();
        if (cur === id || last === id) {
          clearCur();
        }
        toast.success(t('assistant.deleted'));
      } catch (err) {
        console.error('[AIAssistantPanel] 删除会话失败:', err);
        toast.error(t('assistant.deleteFailed'));
      }
    },
    [setSessions, t]
  );

  // 传给 MessageBubble 的 Plan 展示状态
  const planState: PlanDisplayState = {
    active: planActive,
    goal: planGoal,
    steps: planSteps,
    completedSteps: planCompletedSteps,
    totalSteps: planTotalSteps,
    isComplete: planComplete,
    currentTool,
    reflectStage: planReflectStage,
    reflectRound: planReflectRound,
  };

  const selectedNoteIds = new Set(selectedNotes.map((n) => n.id));

  return {
    // 输入与流式状态
    input,
    setInput,
    isStreaming,
    stopStreaming: stop,
    enableThinking,
    setEnableThinking,
    thinkingText,
    loadingMessages,
    sessionsLoading,
    // 附件
    pendingAttachments,
    fileInputRef,
    handleFileSelect,
    removeAttachment,
    // 引用笔记 / PPT 模板
    selectedNotes,
    toggleNote,
    removeNote,
    selectedNoteIds,
    showNotePicker,
    setShowNotePicker,
    // 自动关联（v2）：当前详情区笔记作为默认上下文
    autoNote,
    effectiveNotes,
    dismissAutoNote,
    selectedTemplate,
    toggleTemplate,
    showTemplatePicker,
    setShowTemplatePicker,
    // 消息与会话
    messages,
    sessions,
    currentSessionId,
    planState,
    // 动作
    handleSend,
    handleNewChat,
    refreshSessions,
    loadSession,
    renameSession,
    deleteSession,
  };
}
