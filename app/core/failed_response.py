"""
统一失败响应封装模块

定义业务错误码体系和失败响应构造函数。
错误码分段：
- 400xx: 请求参数错误
- 401xx: 认证失败
- 403xx: 权限不足
- 404xx: 资源不存在
- 409xx: 冲突
- 429xx: 限流
- 500xx: 服务端错误
"""

from typing import Any, Optional
from app.core.request_context import current_request_id


# ========== 错误码常量定义 ==========

class ErrorCode:
    """业务错误码常量集合"""
    
    # 成功
    SUCCESS = 0
    
    # 400xx - 请求参数错误
    MISSING_REQUIRED_FIELD = 40001      # 缺少必填字段
    FORMAT_VALIDATION_FAILED = 40002    # 格式校验失败
    INVALID_PARAMETER = 40003           # 参数值无效
    FILE_TOO_LARGE = 40004              # 文件超过大小限制
    UNSUPPORTED_FILE_TYPE = 40005       # 不支持的文件类型
    EMAIL_CODE_INVALID = 40006          # 邮箱验证码错误或已过期
    EMAIL_SEND_FAILED = 40007           # 邮件发送失败
    MODEL_CONTEXT_EXCEEDED = 40008
    
    # 401xx - 认证失败
    TOKEN_EXPIRED = 40101               # Access Token 已过期
    TOKEN_INVALID = 40102               # Token 无效
    PASSWORD_ERROR = 40103              # 密码错误
    ACCOUNT_LOCKED = 40104              # 账户已锁定（登录失败次数过多）
    REFRESH_TOKEN_INVALID = 40105       # Refresh Token 无效或已失效
    
    # 403xx - 权限不足
    NO_NOTE_ACCESS = 40301              # 无权访问该笔记
    STORAGE_QUOTA_EXCEEDED = 40302      # 上传配额已满
    SESSION_LIMIT_REACHED = 40303       # 会话数量达到上限
    
    # 404xx - 资源不存在
    NOTE_NOT_FOUND = 40401              # 笔记不存在
    SESSION_NOT_FOUND = 40402           # 会话不存在
    USER_NOT_FOUND = 40403              # 用户不存在
    DOCUMENT_NOT_FOUND = 40404          # 知识库文档不存在
    DEVICE_SESSION_NOT_FOUND = 40405    # 设备会话不存在
    ATTACHMENT_NOT_FOUND = 40406        # 聊天附件不存在
    CATEGORY_NOT_FOUND = 40407          # 分类不存在

    # 409xx - 冲突
    USERNAME_EXISTS = 40901             # 用户名已存在
    IDEMPOTENCY_DUPLICATE = 40902       # 幂等键重复（消息重复提交）
    EMAIL_EXISTS = 40903                # 邮箱已被占用
    CATEGORY_NAME_EXISTS = 40904        # 同级分类下已存在同名分类
    
    # 429xx - 限流
    GLOBAL_RATE_LIMIT = 42901           # 全局频率限制
    ENDPOINT_RATE_LIMIT = 42902         # 单接口频率限制
    MODEL_BUDGET_EXCEEDED = 42903
    
    # 500xx - 服务端错误
    INTERNAL_ERROR = 50001              # 内部错误
    LLM_CALL_FAILED = 50002             # LLM 调用失败
    BACKGROUND_CAPACITY_EXCEEDED = 50306
    RAG_UNAVAILABLE = 50307


# 错误码对应的默认消息
_ERROR_MESSAGES = {
    ErrorCode.MISSING_REQUIRED_FIELD: "缺少必填字段",
    ErrorCode.FORMAT_VALIDATION_FAILED: "格式校验失败",
    ErrorCode.INVALID_PARAMETER: "参数值无效",
    ErrorCode.FILE_TOO_LARGE: "文件超过大小限制",
    ErrorCode.UNSUPPORTED_FILE_TYPE: "不支持的文件类型",
    ErrorCode.EMAIL_CODE_INVALID: "验证码错误或已过期",
    ErrorCode.EMAIL_SEND_FAILED: "邮件发送失败，请稍后重试",
    ErrorCode.MODEL_CONTEXT_EXCEEDED: "本轮内容超出模型上下文窗口，请缩短问题或减少附件",
    ErrorCode.TOKEN_EXPIRED: "Access Token 已过期",
    ErrorCode.TOKEN_INVALID: "Token 无效",
    ErrorCode.PASSWORD_ERROR: "密码错误",
    ErrorCode.ACCOUNT_LOCKED: "账户已锁定，请 15 分钟后重试",
    ErrorCode.REFRESH_TOKEN_INVALID: "Refresh Token 无效或已失效",
    ErrorCode.NO_NOTE_ACCESS: "无权访问该笔记",
    ErrorCode.STORAGE_QUOTA_EXCEEDED: "上传配额已满",
    ErrorCode.SESSION_LIMIT_REACHED: "活跃会话数量达到上限",
    ErrorCode.NOTE_NOT_FOUND: "笔记不存在",
    ErrorCode.SESSION_NOT_FOUND: "会话不存在",
    ErrorCode.USER_NOT_FOUND: "用户不存在",
    ErrorCode.DOCUMENT_NOT_FOUND: "知识库文档不存在",
    ErrorCode.DEVICE_SESSION_NOT_FOUND: "设备会话不存在",
    ErrorCode.ATTACHMENT_NOT_FOUND: "聊天附件不存在",
    ErrorCode.CATEGORY_NOT_FOUND: "分类不存在",
    ErrorCode.USERNAME_EXISTS: "用户名已存在",
    ErrorCode.IDEMPOTENCY_DUPLICATE: "请求重复提交",
    ErrorCode.EMAIL_EXISTS: "该邮箱已被使用",
    ErrorCode.CATEGORY_NAME_EXISTS: "同级分类下已存在同名分类",
    ErrorCode.GLOBAL_RATE_LIMIT: "请求频率过高，请稍后再试",
    ErrorCode.ENDPOINT_RATE_LIMIT: "该接口请求频率过高，请稍后再试",
    ErrorCode.MODEL_BUDGET_EXCEEDED: "本次任务的模型调用超出预算或缺少可计价额度",
    ErrorCode.INTERNAL_ERROR: "服务器内部错误",
    ErrorCode.LLM_CALL_FAILED: "AI 模型调用失败",
    ErrorCode.BACKGROUND_CAPACITY_EXCEEDED: "当前任务较多，请稍后重试",
    ErrorCode.RAG_UNAVAILABLE: "知识检索暂不可用，请稍后重试",
}


class BusinessError(Exception):
    """
    业务异常基类
    
    所有业务逻辑错误都应抛出此异常，由全局异常处理器统一捕获并转换为标准响应格式。
    
    Attributes:
        code: 业务错误码
        message: 错误描述信息
        detail: 可选的详细错误信息（用于开发调试）
        http_status: 对应的 HTTP 状态码
    """
    
    def __init__(
        self,
        code: int,
        message: Optional[str] = None,
        detail: Optional[str] = None,
        http_status: int = 400,
        data: Any = None,
    ):
        self.code = code
        self.message = message or _ERROR_MESSAGES.get(code, "未知错误")
        self.detail = detail
        self.http_status = http_status
        self.data = data
        super().__init__(self.message)


def failed_response(
    code: int,
    message: Optional[str] = None,
    detail: Optional[str] = None,
    request_id: Optional[str] = None,
    data: Any = None,
) -> dict:
    """
    构造统一格式的失败响应
    
    Args:
        code: 业务错误码（参考 ErrorCode 类）
        message: 错误消息，不传则使用错误码对应的默认消息
        detail: 可选的详细错误信息
        request_id: 请求唯一标识，不传则自动生成 UUID
        
    Returns:
        统一格式的失败响应字典
    """
    return {
        "code": code,
        "message": message or _ERROR_MESSAGES.get(code, "未知错误"),
        "detail": detail,
        "data": data,
        "request_id": request_id or current_request_id()
    }
