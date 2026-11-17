"""
统一成功响应封装模块

所有 API 成功响应都使用 success_response() 函数封装，
确保返回格式统一为：
{
    "code": 0,
    "message": "ok",
    "data": { ... },
    "request_id": "uuid"
}
"""

from typing import Any, Optional
from app.core.request_context import current_request_id


def success_response(
    data: Any = None,
    message: str = "ok",
    code: int = 0,
    request_id: Optional[str] = None
) -> dict:
    """
    构造统一格式的成功响应
    
    Args:
        data: 响应数据，可以是任意类型（dict、list、基本类型等）
        message: 响应消息，默认为 "ok"
        code: 业务状态码，0 表示成功
        request_id: 请求唯一标识，不传则自动生成 UUID
        
    Returns:
        统一格式的响应字典
    """
    return {
        "code": code,
        "message": message,
        "data": data,
        "request_id": request_id or current_request_id()
    }
