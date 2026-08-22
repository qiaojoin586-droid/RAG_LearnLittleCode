/**
 * API 端点路径字典
 * 
 * 集中管理所有后端 API 路径，与后端路由严格对齐。
 * 后端路由统一前缀：/api/v1（健康检查除外）
 * 
 * 修改后端路由时只需在此处统一修改，前端所有调用方自动同步。
 */

const API_PREFIX = '/api/v1';

export const endpoints = {
  // ========== 认证 ==========
  auth: {
    register: `${API_PREFIX}/auth/register`,
    login: `${API_PREFIX}/auth/login`,
    logout: `${API_PREFIX}/auth/logout`,
    refresh: `${API_PREFIX}/auth/refresh`,
    sseToken: `${API_PREFIX}/auth/sse-token`,
    /** POST 发送邮箱验证码（注册 / 修改邮箱复用） */
    sendCode: `${API_PREFIX}/auth/send-code`,
    /** GET 获取活跃设备会话列表 */
    sessions: `${API_PREFIX}/auth/sessions`,
    /** DELETE 撤销指定设备会话 */
    sessionRevoke: (deviceId: string) => `${API_PREFIX}/auth/sessions/${deviceId}`,
  },

  // ========== PPT ==========
  ppt: {
    /** POST 上传 PPT 模板（multipart: file + name，设计方案 §6.5） */
    templateUpload: `${API_PREFIX}/ppt-template/upload`,
    /** GET 模板列表 */
    templateBase: `${API_PREFIX}/ppt-template`,
    /** DELETE 删除模板 */
    templateDetail: (id: number) => `${API_PREFIX}/ppt-template/${id}`,
  },

  // ========== 用户 ==========
  user: {
    me: `${API_PREFIX}/user/me`,
    password: `${API_PREFIX}/user/me/password`,
    avatar: `${API_PREFIX}/file/avatar`,
    /** POST 修改/绑定邮箱（两步流程第二步） */
    changeEmail: `${API_PREFIX}/user/change-email`,
  },

  // ========== 笔记 ==========
  note: {
    /** POST 创建笔记 / GET 笔记列表 */
    base: `${API_PREFIX}/note`,
    /** GET/PUT/DELETE 笔记详情 */
    detail: (id: string) => `${API_PREFIX}/note/${id}`,
    search: `${API_PREFIX}/note/search`,
    batch: `${API_PREFIX}/note/batch`,
    autocomplete: `${API_PREFIX}/note/autocomplete`,
    writeAssistant: `${API_PREFIX}/note/write-assistant`,
    /** GET 回收站列表 */
    recycleBin: `${API_PREFIX}/note/recycle-bin`,
    /** POST 恢复笔记 */
    restore: (id: string) => `${API_PREFIX}/note/${id}/restore`,
    /** DELETE 彻底删除笔记 */
    permanent: (id: string) => `${API_PREFIX}/note/${id}/permanent`,
    /** PUT 移动笔记到分类（category_id 为查询参数，不传 = 移为未分类） */
    moveCategory: (id: string) => `${API_PREFIX}/note/${id}/category`,
  },

  // ========== 分类 ==========
  category: {
    /** GET 分类树 */
    tree: `${API_PREFIX}/category/tree`,
    /** POST 创建分类 */
    base: `${API_PREFIX}/category`,
    /** PUT 更新 / DELETE 软删除分类 */
    detail: (id: string) => `${API_PREFIX}/category/${id}`,
    /** POST 移动分类（改父级） */
    move: (id: string) => `${API_PREFIX}/category/${id}/move`,
    /** POST 从回收站恢复分类 */
    restore: (id: string) => `${API_PREFIX}/category/${id}/restore`,
    /** DELETE 彻底删除分类（回收站，物理删除不可恢复） */
    permanent: (id: string) => `${API_PREFIX}/category/${id}/permanent`,
    /** POST 批量重排同级分类顺序 */
    reorder: `${API_PREFIX}/category/reorder`,
    /** POST 批量操作（删除/合并） */
    batch: `${API_PREFIX}/category/batch`,
    /** GET 回收站分类列表 */
    recycleBin: `${API_PREFIX}/category/recycle-bin`,
  },

  // ========== 聊天 ==========
  chat: {
    query: `${API_PREFIX}/chat/query`,
    rag: `${API_PREFIX}/chat/rag`,
    sessions: `${API_PREFIX}/chat/sessions`,
    /** DELETE 删除会话 */
    sessionDetail: (sessionId: string) => `${API_PREFIX}/chat/sessions/${sessionId}`,
    /** GET 消息历史（游标分页） */
    messages: (sessionId: string) => `${API_PREFIX}/chat/${sessionId}/messages`,
    /** PUT 修改会话标题 */
    sessionTitle: (sessionId: string) => `${API_PREFIX}/chat/${sessionId}/title`,
    /** POST 上传聊天附件（图片/视频） */
    fileUpload: `${API_PREFIX}/chat/files`,
    /** GET/DELETE 附件详情（预览/删除；回显时拼 ?token=） */
    fileDetail: (fileId: string) => `${API_PREFIX}/chat/files/${fileId}`,
  },

  // ========== 知识库 ==========
  knowledge: {
    upload: `${API_PREFIX}/knowledge/upload`,
    documents: `${API_PREFIX}/knowledge/documents`,
    /** GET/DELETE 文档详情 */
    documentDetail: (docId: number) => `${API_PREFIX}/knowledge/documents/${docId}`,
  },

  // ========== 回顾 ==========
  review: {
    today: `${API_PREFIX}/review/today`,
    /** POST 标记回顾完成 */
    complete: (reviewId: number) => `${API_PREFIX}/review/${reviewId}/complete`,
    stats: `${API_PREFIX}/review/stats`,
  },

  // ========== 模板 ==========
  template: {
    /** POST 创建模板 / GET 模板列表 */
    base: `${API_PREFIX}/note-template`,
    /** GET/PUT/DELETE 模板详情 */
    detail: (templateId: number) => `${API_PREFIX}/note-template/${templateId}`,
  },
} as const;
