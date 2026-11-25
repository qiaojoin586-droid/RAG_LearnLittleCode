/**
 * 消息气泡（用户/AI 消息统一渲染）
 *
 * 职责：
 * - 头像（AI 机器人 / 用户头像）
 * - AI 消息：Plan 进度卡片、思考加载态、Markdown 内容（PPT 完成时折叠）、
 *   工具调用状态指示、PPT/TTS 下载卡片
 * - 用户消息：纯文本 + 附件预览（AttachmentViewer）
 *
 * 审查 ⑤：从 AIChat.tsx（1088 行巨型组件）拆出。
 */

import { useMemo } from 'react';
import { Loader2, User } from 'lucide-react';
import type { ChatMessage } from '../../types/api';
import type { PlanStepData } from './PlanProgressCard';
import PlanProgressCard from './PlanProgressCard';
import MessageContent from './MessageContent';
import ReferencedNoteCards from './ReferencedNoteCards';
import { parseReferencedNotes } from '../../utils/noteReferenceParser';
import PptDownloadCard from './PptDownloadCard';
import TtsAudioCard from './TtsAudioCard';
import AttachmentViewer from './AttachmentViewer';
import AnswerCitations from './AnswerCitations';

/** 工具名 → 中文展示名（简单路径补转发工具事件后的状态指示，§7） */
const TOOL_NAME_MAP: Record<string, string> = {
  generate_ppt_tool: '正在生成 PPT（约需 1~2 分钟，Aspose 云端渲染较慢）…',
  search_notes_tool: '正在搜索笔记…',
  get_note_content_tool: '正在读取笔记内容…',
  get_note_stats_tool: '正在统计笔记…',
  get_today_reviews_tool: '正在获取待回顾笔记…',
  mark_reviewed_tool: '正在标记回顾…',
  create_note_tool: '正在创建笔记…',
  update_note_tool: '正在更新笔记…',
  get_related_notes_tool: '正在查找关联笔记…',
  get_user_info_tools: '正在读取用户信息…',
  send_email: '正在发送邮件…',
  what_time_is_now: '正在获取时间…',
};

/** Plan-and-Execute 进度状态（由页面统一持有，传入展示） */
export interface PlanDisplayState {
  /** Plan 模式是否激活（未激活时进度卡片不展示） */
  active: boolean;
  goal: string;
  steps: PlanStepData[];
  completedSteps: number;
  totalSteps: number;
  isComplete: boolean;
  currentTool: string;
  /** 反思阶段（L1 综合后自检）：'' | 'checking' | 'refining' */
  reflectStage?: string;
  /** 反思修正轮次 */
  reflectRound?: number;
}

interface Props {
  msg: ChatMessage;
  /** 是否为最后一条消息（决定是否展示 Plan 进度卡片与工具状态指示） */
  isLast: boolean;
  isStreaming: boolean;
  /** 用户头像 URL（store 中可能为 null） */
  userAvatar?: string | null;
  plan: PlanDisplayState;
  /** 深度思考模式的实时思考内容（thinking 事件，审查 B+C） */
  thinkingText?: string;
}

/**
 * 用户消息内容渲染（v2 引用笔记卡片化）
 *
 * 用户消息原为纯文本直出（whitespace-pre-wrap）；现解析引用块：
 * - 原始问题保持纯文本样式（蓝底白字）
 * - 引用笔记替换为卡片列表（与 AI 搜索结果卡片同族，点击预览全文）
 * - 解析失败（普通消息）→ 原样纯文本
 */
function UserMessageContent({ content }: { content: string }) {
  const parts = useMemo(() => parseReferencedNotes(content), [content]);

  if (!parts) {
    return <div className="text-sm whitespace-pre-wrap">{content}</div>;
  }

  return (
    <div className="text-sm">
      {parts.prefix.trim() && <div className="whitespace-pre-wrap">{parts.prefix}</div>}
      <ReferencedNoteCards notes={parts.notes} />
      {parts.suffix.trim() && <div className="whitespace-pre-wrap">{parts.suffix}</div>}
    </div>
  );
}

export default function MessageBubble({ msg, isLast, isStreaming, userAvatar, plan, thinkingText }: Props) {
  const isUser = msg.role === 'user';

  return (
    <div className={`flex items-start gap-2 ${isUser ? 'justify-end' : 'justify-start'} ai-msg-enter`}>
      {!isUser && (
        <div className="flex-shrink-0 w-8 h-8 rounded-full overflow-hidden ring-2 ring-[#4a9eff]/15 shadow-[0_0_8px_rgba(74,158,255,0.12)]">
          <img src="/avatars/xiaoyunyun.png" alt="小云云" className="w-full h-full object-cover" />
        </div>
      )}
      <div className={`max-w-[80%] p-3 rounded-[var(--radius-md)] ${
        isUser
          ? 'bg-[var(--color-accent)] text-white'
          : 'border-l-[3px] border-l-[#4a9eff]/40 text-[var(--color-text)]'
      }`} style={!isUser ? { background: 'var(--color-assistant-ai-bubble)', borderLeftColor: 'var(--color-assistant-ai-bubble-border)' } : undefined}>
        {/* Plan 进度卡片（仅在最后一条 AI 消息上显示） */}
        {!isUser && isLast && plan.active && (
          <PlanProgressCard
            goal={plan.goal}
            steps={plan.steps}
            completedSteps={plan.completedSteps}
            totalSteps={plan.totalSteps}
            isComplete={plan.isComplete}
            currentTool={plan.currentTool}
            reflectStatus={plan.reflectStage}
            reflectRound={plan.reflectRound}
          />
        )}
        {/* AI 思考加载态：内容为空时显示"正在思考" + 3 个跳动圆点（demo 规格一致）；
            深度思考模式附加实时思考内容（审查 B+C，最多展示 3 行避免刷屏） */}
        {!isUser && !msg.content && (
          <div className="thinking-loading" role="status" aria-live="polite">
            <span>正在思考</span>
            <span className="thinking-dots" aria-hidden="true">
              <span className="thinking-dot" />
              <span className="thinking-dot" />
              <span className="thinking-dot" />
            </span>
            {thinkingText && (
              <div className="mt-1.5 text-xs text-[var(--color-text-tertiary)] whitespace-pre-wrap line-clamp-3 leading-relaxed">
                {thinkingText}
              </div>
            )}
          </div>
        )}
        {!isUser ? (
          // v1.6：生成完成且含 PPT 卡片时，冗长过程文本折叠为「查看生成过程」
          // （生成中仍实时显示；避免「校验报告+设计稿+结果」平铺刷屏）
          msg.attachments?.some((a) => a.file_type === 'ppt') && !isStreaming ? (
            <details className="group">
              <summary className="cursor-pointer select-none text-xs text-[var(--color-text-tertiary)] hover:text-[var(--color-accent)]">
                查看生成过程
              </summary>
              <div className="mt-2">
                <MessageContent content={msg.content || '...'} />
              </div>
            </details>
          ) : (
            <MessageContent content={msg.content || '...'} />
          )
        ) : (
          /* 用户消息：引用笔记卡片化（v2）——解析引用块，问题保留纯文本样式，
             引用笔记以卡片展示（卡片为白底，在蓝色气泡上仍可读） */
          <UserMessageContent content={msg.content || '...'} />
        )}
        {!isUser && !!msg.citation_ids?.length && (
          <AnswerCitations session={msg.session_id} message={msg.id} ids={msg.citation_ids} />
        )}
        {/* 工具调用状态指示（简单路径补转发工具事件后生效，§6.3/§7；中文名映射） */}
        {!isUser && isLast && plan.currentTool && (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
            <Loader2 size={12} className="animate-spin" />
            <span>{TOOL_NAME_MAP[plan.currentTool] ?? plan.currentTool}</span>
          </div>
        )}
        {/* PPT 生成完成下载卡片（v1.6：从消息 attachments 渲染——流式挂载与历史回放同源，切换模块不消失） */}
        {msg.attachments?.find((a) => a.file_type === 'ppt') && (
          <PptDownloadCard file={msg.attachments.find((a) => a.file_type === 'ppt')!} />
        )}
        {/* TTS 语音播放/下载卡片（v1.7：外部 API 工具文档 §10 C3，同 attachments 渲染源） */}
        {msg.attachments?.find((a) => a.file_type === 'tts') && (
          <TtsAudioCard file={msg.attachments.find((a) => a.file_type === 'tts')!} />
        )}
        {/* 用户消息附件渲染（图片缩略图 / 视频播放器，历史回显同样走这里） */}
        {isUser && msg.attachments && msg.attachments.length > 0 && (
          <AttachmentViewer attachments={msg.attachments} />
        )}
      </div>
      {isUser && (
        <div className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center bg-[var(--color-accent)] overflow-hidden">
          {userAvatar ? (
            <img src={userAvatar} alt="avatar" className="w-8 h-8 rounded-full object-cover" />
          ) : (
            <User size={18} className="text-white" />
          )}
        </div>
      )}
    </div>
  );
}
