"""
全局异常处理器注册模块

注册 FastAPI 全局异常处理器，统一捕获并处理：
- BusinessError: 业务异常，转换为标准错误响应格式
- RequestValidationError: 请求参数校验异常（Pydantic 校验失败）
- Exception: 未预期的异常，返回 500 内部错误

所有异常响应都包含 request_id 用于问题追踪。
"""

import traceback
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, failed_response, ErrorCode
from app.core.request_context import RequestIdMiddleware


def register_exception_handlers(app: FastAPI) -> None:
    """
    注册全局异常处理器到 FastAPI 应用
    
    Args:
        app: FastAPI 应用实例
    """
    
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code", {
            401: ErrorCode.TOKEN_INVALID, 429: ErrorCode.ENDPOINT_RATE_LIMIT,
        }.get(exc.status_code, ErrorCode.INVALID_PARAMETER))
        identifier = request.state.request_id
        return JSONResponse(status_code=exc.status_code, headers={
            **(exc.headers or {}), "X-Request-ID": identifier,
        }, content=failed_response(
            code=code, message=detail.get("message") or "请求未能完成",
            request_id=identifier, data={"reason": "HTTP_ERROR"},
        ))

    @app.exception_handler(BusinessError)
    async def business_error_handler(request: Request, exc: BusinessError):
        """
        处理业务异常
        
        将 BusinessError 转换为标准格式的 JSON 响应，
        同时记录警告日志（包含请求路径和错误详情）。
        """
        logger.warning(
            f"业务异常: {request.method} {request.url.path} - "
            f"code={exc.code}, message={exc.message}"
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=failed_response(
                code=exc.code,
                message=exc.message,
                detail=exc.detail,
                data=exc.data,
                request_id=request.state.request_id,
            )
        )
    
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """
        处理请求参数校验异常
        
        当 Pydantic Schema 校验失败时触发，
        将校验错误信息提取为可读的详情字符串。
        """
        # 提取校验错误详情
        errors = []
        for error in exc.errors():
            field = " -> ".join(str(loc) for loc in error["loc"])
            errors.append(f"{field}: {error['type']}")
        
        detail = "; ".join(errors)
        
        logger.warning(
            f"参数校验失败: {request.method} {request.url.path} - {detail}"
        )
        
        return JSONResponse(
            status_code=422,
            content=failed_response(
                code=ErrorCode.FORMAT_VALIDATION_FAILED,
                message="请求参数校验失败",
                detail=detail,
                data={"reason": "FORMAT_VALIDATION_FAILED"},
                request_id=request.state.request_id,
            )
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """
        处理未预期的异常（兜底）
        
        捕获所有未被其他处理器处理的异常，
        记录完整的错误堆栈信息用于排查问题。
        """
        # 记录完整错误堆栈
        error_traceback = traceback.format_exc()
        logger.error(
            f"未预期异常: {request.method} {request.url.path}\n"
            f"{error_traceback}"
        )
        
        return JSONResponse(
            status_code=500,
            headers={"X-Request-ID": request.state.request_id},
            content=failed_response(
                code=ErrorCode.INTERNAL_ERROR,
                message="服务器内部错误",
                request_id=request.state.request_id,
            )
        )
