/**
 * 全局 API 类型定义
 * 
 * 与后端 Pydantic Schema 一一对应，确保前后端数据结构一致。
 */

// ========== 通用响应 ==========

/** 后端统一响应格式 */
export interface ApiResponse<T = unknown> {
  code: number;
  message: string;
  data: T;
  request_id: string;
}

// ========== 用户 & 认证 ==========

/** 用户注册请求 */
export interface UserRegister {
  username: string;
  password: string;
  /** 邮箱（必填，需通过验证码验证） */
  email: string;
  /** 6 位邮箱验证码 */
  verification_code: string;
}

/** 发送邮箱验证码请求（注册 / 修改邮箱复用） */
export interface SendCodeRequest {
  email: string;
}

/** 修改邮箱请求 */
export interface EmailChangeRequest {
  email: string;
  verification_code: string;
}

/** 用户登录请求 */
export interface UserLogin {
  username: string;
  password: string;
  /** 设备唯一标识（前端生成并持久化） */
  device_id?: string;
  /** 设备可读名称，如 Chrome on Windows */
  device_name?: string;
}

/** Token 响应 */
export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  /** 回传设备标识，前端应持久化存储 */
  device_id?: string;
}

/** 用户信息 */
export interface UserInfo {
  uuid: string;
  username: string;
  email: string | null;
  /** 邮箱是否已验证 */
  email_verified: boolean;
  avatar: string | null;
  bio: string | null;
  status: string;
  created_at: string;
}

/** 用户信息更新（email 已移除 → 邮箱变更请使用 EmailChangeRequest） */
export interface UserUpdate {
  bio?: string;
}

/** 密码修改 */
export interface PasswordChange {
  old_password: string;
  new_password: string;
}

/** 设备会话信息 */
export interface SessionInfo {
  device_id: string;
  device_name?: string;
  ip?: string;
  created_at?: string;
  last_used?: string;
  is_current: boolean;
}

// ========== 笔记 ==========

/** 笔记响应 */
export interface Note {
  id: string;
  user_id: string;
  title: string;
  content: string;
  tags: string[] | null;
  category: string | null;
  /** 分类 ID（双写期间与 category 并行返回） */
  category_id: string | null;
  is_pinned: boolean;
  /** 文档类型：md = Markdown / txt = 纯文本（旧数据兼容默认 'md'） */
  format: string;
  created_at: string;
  updated_at: string;
  /** 删除时间（回收站中的笔记有值，正常笔记为 null） */
  deleted_at?: string | null;
}

/** 回收站笔记响应（附带距离自动彻底删除的剩余天数） */
export interface DeletedNote extends Note {
  days_remaining: number;
}

/** 回收站列表响应 */
export interface DeletedNoteListResponse {
  notes: DeletedNote[];
  total: number;
  page: number;
  page_size: number;
}

/** 创建笔记请求 */
export interface NoteCreate {
  title: string;
  content: string;
  tags?: string[];
  category?: string;
  /** 分类 ID（优先；后端自动同步 category 字符串） */
  category_id?: string;
  is_pinned?: boolean;
  /** 文档类型：md = Markdown（默认）/ txt = 纯文本；仅新建时写入 */
  format?: 'md' | 'txt';
}

/** 更新笔记请求 */
export interface NoteUpdate {
  title?: string;
  content?: string;
  tags?: string[];
  category?: string;
  /** 分类 ID（显式传 null 表示移为未分类） */
  category_id?: string | null;
  is_pinned?: boolean;
}

/** 笔记列表响应 */
export interface NoteListResponse {
  notes: Note[];
  total: number;
  page: number;
  page_size: number;
}

/** 语义搜索请求 */
export interface NoteSearchRequest {
  query: string;
  top_k?: number;
}

/** 语义搜索结果项 */
export interface NoteSearchResult {
  note: Note;
  score: number;
}

/** 批量操作请求 */
export interface BatchOperation {
  note_ids: string[];
  operation: 'delete' | 'pin' | 'unpin' | 'move' | 'permanent_delete' | 'restore';
  target_category?: string;
  /** 目标分类 ID（move 操作时优先使用） */
  target_category_id?: string;
}

// ========== 聊天 ==========

/** 聊天附件元数据（对应后端 AttachmentMeta） */
export interface AttachmentMeta {
  file_id: string;
  file_type: 'image' | 'video' | 'ppt' | 'tts';
  original_name: string;
  file_size?: number;
  mime_type?: string;
  width?: number;
  height?: number;
  duration_sec?: number | null;
  // PPT 工具文件（file_type='ppt' 时）：随消息持久化，历史回放恢复下载卡片
  download_url?: string;
  title?: string;
  slide_count?: number;
  // TTS 音频（file_type='tts' 时）
  duration_estimate?: string;
}

/** 附件上传响应（对应后端 UploadResponse） */
export interface UploadResponse {
  file_id: string;
  file_type: 'image' | 'video';
  mime_type: string;
  original_name: string;
  file_size: number;
  width?: number | null;
  height?: number | null;
  duration_sec?: number | null;
  created_at: string;
}

/** 聊天消息 */
export interface ChatMessage {
  id: number;
  session_id: string;
  role: string;
  content: string;
  token_count?: number;
  created_at: string;
  /** 附件元数据（用户消息携带） */
  attachments?: AttachmentMeta[];
}

/** 聊天会话 */
export interface ChatSession {
  id: string;
  title: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

/** 消息列表响应（游标分页） */
export interface MessageListResponse {
  messages: ChatMessage[];
  has_more: boolean;
  next_cursor: string | null;
}

/** 对话请求 */
export interface QueryRequest {
  session_id?: string;
  /** 用户消息（可空：仅发附件时为空，由后端占位） */
  message?: string;
  idempotency_key?: string;
  /** 深度思考开关 */
  enable_thinking?: boolean;
  /** 附件 ID 列表（先上传后发送） */
  attachment_ids?: string[];
}

/** 会话标题修改 */
export interface SessionTitleUpdate {
  title: string;
}

// ========== 知识库 ==========

/** 知识库文档 */
export interface KnowledgeDocument {
  id: number;
  user_id: string;
  filename: string;
  file_size: number;
  file_type: string;
  md5_hash: string;
  chunk_count: number;
  created_at: string;
  updated_at: string;
}

/** 文档列表响应 */
export interface KnowledgeDocumentListResponse {
  documents: KnowledgeDocument[];
  total: number;
}

// ========== 回顾 ==========

/** 回顾记录 */
export interface ReviewRecord {
  review_id: number;
  note_id: string;
  note_title: string;
  note_content?: string;
  next_review_at: string;
  interval_days: number;
  review_count: number;
}

/** 回顾统计 */
export interface ReviewStats {
  total_reviews: number;
  completed_today: number;
  pending_today: number;
  streak_days: number;
}

// ========== 模板 ==========

/** 笔记模板 */
export interface NoteTemplate {
  id: number;
  user_id: string;
  name: string;
  content_structure: Record<string, unknown> | null;
  category: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

/** 创建模板请求 */
export interface NoteTemplateCreate {
  name: string;
  content_structure?: Record<string, unknown>;
  category?: string;
  sort_order?: number;
}

/** 更新模板请求 */
export interface NoteTemplateUpdate {
  name?: string;
  content_structure?: Record<string, unknown>;
  category?: string;
  sort_order?: number;
}

// ========== SSE 消息类型 ==========

/** 聊天 SSE 消息 */
export interface ChatSSEMessage {
  type: 'thinking' | 'response' | 'done' | 'error'
       | 'plan_start' | 'plan_step' | 'plan_step_start'
       | 'plan_step_end' | 'plan_synthesize' | 'plan_complete'
       | 'plan_fallback'
       | 'tool_start' | 'tool_end' | 'tool_file';
  stage?: string;
  content?: string;
  details?: Record<string, unknown>;
  session_id?: string;
  // Plan-and-Execute 扩展字段
  goal?: string;
  total_steps?: number;
  completed_steps?: number;
  step?: number;
  action?: string;
  status?: 'pending' | 'running' | 'completed' | 'failed';
  result?: string;
  reason?: string;
  // 工具调用字段
  name?: string;
  duration_ms?: number;
  // tool_file 事件字段（PPT 生成完成，§6.3）
  file_id?: string;
  download_url?: string;
  slide_count?: number;
  title?: string;
}

/** tool_file 事件信息（PPT 下载卡片使用） */
export interface ToolFileInfo {
  name?: string;
  file_id: string;
  download_url: string;
  slide_count?: number;
  title?: string;
  // TTS 音频（name='text_to_speech' 时）
  audio_url?: string;
  duration_estimate?: string;
}

/** 知识库 SSE 消息 */
export interface KnowledgeSSEMessage {
  event_type: 'processing' | 'completed' | 'finish' | 'error';
  filename?: string;
  progress?: number;
  stage?: string;
  message?: string;
  document_id?: number;
}
