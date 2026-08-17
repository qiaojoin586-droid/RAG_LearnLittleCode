/**
 * AI 对话页面
 *
 * 功能：
 * 1. SSE 流式对话（思考过程实时展示）
 * 2. Markdown 渲染 AI 回答
 * 3. 消息气泡列表 + 自动滚动
 * 4. 快捷问题按钮
 * 5. 引用笔记 / PPT 模板选择、图片视频附件、Plan 进度卡片
 *
 * 审查 ⑤：巨型组件拆分 —— 渲染层组件已拆至 components/chat/：
 *   MessageBubble / MessageContent / PptDownloadCard / TtsAudioCard /
 *   NotePicker / TemplatePicker；本文件保留状态编排与 SSE 生命周期。
 */

import { useState, useRef, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Send, Square, Plus, FileText, X, Brain, Paperclip, Presentation } from 'lucide-react';
import { useSSE } from '../hooks/useSSE';
import { useSessionStore } from '../stores/useSessionStore';
import { useUserStore } from '../stores/useUserStore';
import { sessionsApi } from '../api/sessions';
import { endpoints } from '../api/endpoints';
import { uploadChatFile, deleteChatFile } from '../api/chat';
import type { ChatMessage, Note, AttachmentMeta } from '../types/api';
import type { PptTemplateInfo } from '../api/pptTemplates';
import type { PlanStepData } from '../components/chat/PlanProgressCard';
import type { PlanDisplayState } from '../components/chat/MessageBubble';
import MessageBubble from '../components/chat/MessageBubble';
import AttachmentBar from '../components/chat/AttachmentBar';
import type { PendingAttachment } from '../components/chat/AttachmentBar';
import NotePicker from '../components/chat/NotePicker';
import TemplatePicker from '../components/chat/TemplatePicker';

// 附件限制（与后端 .env 配置保持一致）
const MAX_IMAGES_PER_MSG = 6;
const MAX_VIDEOS_PER_MSG = 1;
const MAX_IMAGE_MB = 10;
const MAX_VIDEO_MB = 50;

export default function AIChat() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { start, stop } = useSSE();

  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  // 思考阶段日志仍在 SSE 回调中收集（保留收集便于将来恢复展示）
  const [, setThinkingStages] = useState<{ stage: string; content: string }[]>([]);
  // 深度思考模式的实时思考内容（thinking 事件，审查 B+C：显示思考进度消除等待焦虑）
  const [thinkingText, setThinkingText] = useState('');
  // 深度思考开关（默认关闭；开启后请求带 enable_thinking=true，后端切换思考模型）
  const [enableThinking, setEnableThinking] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // 引用笔记状态
  const [selectedNotes, setSelectedNotes] = useState<Note[]>([]);
  const [showNotePicker, setShowNotePicker] = useState(false);

  // PPT 模板选择状态（v1.4，设计方案 §6.5：与笔记一起选中，单选）
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
  const addMessage = useSessionStore((s) => s.addMessage);
  const setMessages = useSessionStore((s) => s.setMessages);
  const updateLastAssistantMessage = useSessionStore((s) => s.updateLastAssistantMessage);
  const clearCurrentSession = useSessionStore((s) => s.clearCurrentSession);
  const setLastSessionId = useSessionStore((s) => s.setLastSessionId);
  const userAvatar = useUserStore((s) => s.userInfo?.avatar);

  /** 进入会话时加载历史消息，或恢复最近会话 */
  useEffect(() => {
    if (!sessionId) {
      // /chat 无 sessionId：通过 getState() 读取最新 lastSessionId（不加入依赖数组，避免状态变化触发 effect）
      const lastId = useSessionStore.getState().lastSessionId;
      if (lastId) {
        navigate(`/chat/${lastId}`, { replace: true });
        return;
      }
      // 无最近会话：真正的新对话，清空状态
      clearCurrentSession();
      return;
    }

    // 有 sessionId：记录为最近会话并加载消息
    setLastSessionId(sessionId);

    let cancelled = false;
    (async () => {
      setLoadingMessages(true);
      try {
        const res = await sessionsApi.getMessages(sessionId);
        if (cancelled) return;
        const msgs = res.data?.data?.messages ?? [];
        setMessages(msgs);
      } catch (err) {
        if (!cancelled) {
          // 404 表示会话已被删除，清除残留状态并回退到空白新对话
          const status = (err as { response?: { status?: number } })?.response?.status;
          if (status === 404) {
            console.warn('[AIChat] 会话不存在(404)，清除残留状态');
            clearCurrentSession();
            navigate('/chat', { replace: true });
          } else {
            console.error('[AIChat] 加载历史消息失败:', err);
          }
        }
      } finally {
        if (!cancelled) setLoadingMessages(false);
      }
    })();

    return () => { cancelled = true; };
    // navigate/setLastSessionId/clearCurrentSession/setMessages 均为稳定引用（router/zustand）
  }, [sessionId, navigate, setLastSessionId, clearCurrentSession, setMessages]);

  /** 自动滚动到底部 */
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

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
        console.error('[AIChat] 附件上传失败:', err);
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
        console.warn('[AIChat] 附件删除失败:', err)
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

    // 构建发送内容：若有引用笔记，将笔记内容作为上下文附加（含 ID 供 Agent 直接更新）
    let messageText = input;
    if (selectedNotes.length > 0) {
      const noteContext = selectedNotes
        .map((n) => `【笔记：${n.title}】\n${n.content}`)
        .join('\n\n');
      // 结构化引用块：包含笔记 ID，供后端解析并注入 Agent 上下文
      const noteRefBlock = selectedNotes
        .map((n) => `- ID: ${n.id} | 标题: ${n.title}`)
        .join('\n');
      messageText = `${input}\n\n---\n以下是用户引用的笔记内容，请结合这些内容回答：\n${noteContext}\n\n<referenced_notes>\n${noteRefBlock}\n</referenced_notes>`;
    }
    // 附加用户选择的 PPT 模板（v1.4，§6.5：后端解析注入 system_prompt，LLM 填入工具参数）
    if (selectedTemplate) {
      messageText += `\n\n<ppt_template>\n- ID: ${selectedTemplate.id} | 名称: ${selectedTemplate.name}\n</ppt_template>`;
      // 发送后清除模板选择（模板是一次性使用，避免「没选模板却用了旧模板」的困惑；
      // 与笔记选择保留策略不同）
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
      content: input, // 气泡中只显示用户原始输入
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
          session_id: sessionId,
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
            // 回答开始流出，反思状态使命结束（清空时序 P2-4）
            setPlanReflectStage('');
            setPlanReflectRound(0);
          },
          onDone: (newSessionId) => {
            flushPending();
            setIsStreaming(false);
            setCurrentTool('');
            // 新会话创建后记录并更新 URL
            if (newSessionId) {
              setLastSessionId(newSessionId);
              if (!sessionId) {
                navigate(`/chat/${newSessionId}`, { replace: true });
              }
            }
          },
          onError: (msg) => {
            // 确保错误前内容被刷新
            flushPending();
            updateLastAssistantMessage(`⚠️ ${msg}`);
            setIsStreaming(false);
            setCurrentTool('');
            // 异常路径兜底清空反思状态（P2-4）
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
            // 降级路径兜底清空反思状态（P2-4）
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
          // 工具产出文件事件（PPT 生成完成 §6.3 第 3 段 / TTS 语音生成，外部 API 工具文档 §10 C3）
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

  const quickQuestions = ['Deepseek大模型最新进展', 'AI Agent领域最新进展', '帮我搜索ChatGPT的最新论文并整理成笔记'];

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

  return (
    <div className="flex flex-col h-[calc(100vh-48px)]">
      {/* 头部 */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-heading font-bold text-[var(--color-text)]">
          {t('nav.chat')}
        </h1>
        <button
          onClick={() => {
            clearCurrentSession();
            navigate('/chat');
          }}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-[var(--radius-md)] border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)] transition-colors"
          title="新建对话"
        >
          <Plus size={16} />
          <span>新对话</span>
        </button>
      </div>

      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto space-y-4 mb-4">
        {loadingMessages ? (
          <div className="text-center py-16">
            <p className="text-[var(--color-text-tertiary)]">{t('common.loading')}</p>
          </div>
        ) : messages.length === 0 && !sessionId ? (
          <div className="text-center py-16">
            <p className="text-[var(--color-text-tertiary)] mb-4">💡 快捷问题</p>
            <div className="flex flex-wrap justify-center gap-2">
              {quickQuestions.map((q) => (
                <button
                  key={q}
                  onClick={() => { setInput(q); }}
                  className="px-3 py-1.5 text-sm rounded-full border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {messages.map((msg, idx) => (
          <MessageBubble
            key={msg.id}
            msg={msg}
            isLast={idx === messages.length - 1}
            isStreaming={isStreaming}
            userAvatar={userAvatar}
            plan={planState}
            thinkingText={thinkingText}
          />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* 输入区 */}
      <div className="relative flex gap-2">
        {/* 附件上传按钮（📎） */}
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={isStreaming}
          className={`flex-shrink-0 px-3 py-2.5 rounded-[var(--radius-md)] border transition-colors ${
            pendingAttachments.length > 0
              ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
              : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
          } disabled:opacity-50`}
          title="上传图片/视频"
        >
          <Paperclip size={18} />
        </button>
        {/* 隐藏的文件选择器（图片/视频，可多选） */}
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*,video/*"
          multiple
          hidden
          onChange={handleFileSelect}
        />

        {/* 深度思考开关 */}
        <button
          onClick={() => setEnableThinking(!enableThinking)}
          className={`flex-shrink-0 px-3 py-2.5 rounded-[var(--radius-md)] border transition-colors text-xs font-medium ${
            enableThinking
              ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
              : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
          }`}
          title="深度思考：回答质量更高但响应更慢"
        >
          <Brain size={16} className="inline-block mr-1 align-[-3px]" />
          深度思考
        </button>

        {/* 引用笔记按钮 */}
        <button
          onClick={() => setShowNotePicker((v) => !v)}
          className={`flex-shrink-0 px-3 py-2.5 rounded-[var(--radius-md)] border transition-colors ${
            showNotePicker || selectedNotes.length > 0
              ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
              : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
          }`}
          title="引用笔记"
        >
          <FileText size={18} />
        </button>

        {/* PPT 模板按钮（v1.4：与笔记一起选中，生成 PPT 时使用） */}
        <button
          onClick={() => setShowTemplatePicker((v) => !v)}
          className={`flex-shrink-0 px-3 py-2.5 rounded-[var(--radius-md)] border transition-colors ${
            showTemplatePicker || selectedTemplate
              ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-accent-bg)]'
              : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-accent-bg)] hover:text-[var(--color-accent)]'
          }`}
          title="选择 PPT 模板"
        >
          <Presentation size={18} />
        </button>

        {/* PPT 模板选择面板（单选；懒加载模板列表） */}
        <TemplatePicker
          open={showTemplatePicker}
          onClose={() => setShowTemplatePicker(false)}
          selected={selectedTemplate}
          onSelect={toggleTemplate}
        />

        {/* 笔记选择面板 */}
        <NotePicker
          open={showNotePicker}
          onClose={() => setShowNotePicker(false)}
          selectedIds={selectedNoteIds}
          onToggle={toggleNote}
        />

        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder={t('chat.placeholder')}
          className="flex-1 px-4 py-2.5 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
          disabled={isStreaming}
        />
        {isStreaming ? (
          <button
            onClick={stop}
            className="px-4 py-2.5 rounded-[var(--radius-md)] bg-[var(--color-danger)] text-white hover:opacity-90"
          >
            <Square size={18} />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={
              (!input.trim() &&
                pendingAttachments.filter((a) => a.status === 'done' && a.fileId).length === 0) ||
              pendingAttachments.some((a) => a.status === 'uploading')
            }
            className="px-4 py-2.5 rounded-[var(--radius-md)] bg-[var(--color-accent)] text-white hover:opacity-90 disabled:opacity-50"
          >
            <Send size={18} />
          </button>
        )}
      </div>

      {/* 附件预览条（输入框下方，发送后清空） */}
      <AttachmentBar items={pendingAttachments} onRemove={removeAttachment} />

      {/* 引用笔记标签（输入框下方） */}
      {selectedNotes.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {selectedNotes.map((note) => (
            <span
              key={note.id}
              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-[var(--radius-md)] bg-[var(--color-accent-bg)] text-[var(--color-accent)] border border-[var(--color-accent)]/20"
            >
              <FileText size={12} />
              <span className="max-w-[120px] truncate">{note.title}</span>
              <button onClick={() => removeNote(note.id)} className="hover:opacity-70">
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* PPT 模板标签（v1.4，输入框下方） */}
      {selectedTemplate && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          <span className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-[var(--radius-md)] bg-[var(--color-accent-bg)] text-[var(--color-accent)] border border-[var(--color-accent)]/20">
            <Presentation size={12} />
            <span className="max-w-[160px] truncate">模板：{selectedTemplate.name}</span>
            <button onClick={() => setSelectedTemplate(null)} className="hover:opacity-70">
              <X size={12} />
            </button>
          </span>
        </div>
      )}
    </div>
  );
}
