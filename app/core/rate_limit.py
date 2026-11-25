"""
令牌桶限流模块

实现基于 Redis 的令牌桶限流算法，分两个层级：
- 全局限流：100 次/分钟/用户，防止单用户打爆服务
- 接口级限流：按端点差异化配置，保护高成本接口

限流依赖通过 Depends(rate_limit(...)) 注入到路由中。
"""

import time
from typing import Optional

from fastapi import Request
import redis.asyncio as aioredis

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, ErrorCode

# ========== 接口级限流配置 ==========
# 每个端点的限流配置：次数/分钟
ENDPOINT_RATE_LIMITS = {
    "/api/v1/chat": 10,              # Agent 对话（Ollama 正常）
    "/api/v1/knowledge/upload": 20,  # 知识库上传
    "/api/v1/note": 60,              # 笔记 CRUD
    "/api/v1/auth/send-code": 10,    # 邮箱验证码发送（更长前缀优先匹配，覆盖 /api/v1/auth: 5）
    "/api/v1/auth": 5,               # 登录/注册（防暴力破解）
}

# 默认限流：30 次/分钟
DEFAULT_RATE_LIMIT = 30

# 全局限流：100 次/分钟/用户
GLOBAL_RATE_LIMIT = 100

# 限流窗口时长（秒）
RATE_LIMIT_WINDOW = 60


async def check_rate_limit(
    redis_client: aioredis.Redis,
    user_id: str,
    endpoint: str,
    global_limit: int = GLOBAL_RATE_LIMIT,
    endpoint_limit: Optional[int] = None
) -> bool:
    """
    检查用户是否超过限流阈值（令牌桶算法简化版 - 固定窗口计数）
    
    先检查全局限流，再检查接口级限流。
    使用 Redis INCR + EXPIRE 实现固定窗口计数。
    
    Args:
        redis_client: Redis 异步连接
        user_id: 用户唯一标识
        endpoint: 请求的 API 端点路径
        global_limit: 全局每分钟限流次数
        endpoint_limit: 接口级每分钟限流次数，None 表示使用默认配置
        
    Returns:
        True 表示通过限流检查，False 表示被限流
    """
    current_time = int(time.time())
    window_key_time = current_time // RATE_LIMIT_WINDOW
    
    # 1. 检查全局限流
    global_key = f"rate_limit:{user_id}:global:{window_key_time}"
    global_count = await redis_client.incr(global_key)
    if global_count == 1:
        await redis_client.expire(global_key, RATE_LIMIT_WINDOW + 1)
    
    if global_count > global_limit:
        logger.warning(f"全局限流触发: user_id={user_id}, count={global_count}/{global_limit}")
        return False
    
    # 2. 检查接口级限流
    # 确定该端点的限流阈值
    if endpoint_limit is None:
        endpoint_limit = _get_endpoint_limit(endpoint)
    
    endpoint_key = f"rate_limit:{user_id}:{endpoint}:{window_key_time}"
    endpoint_count = await redis_client.incr(endpoint_key)
    if endpoint_count == 1:
        await redis_client.expire(endpoint_key, RATE_LIMIT_WINDOW + 1)
    
    if endpoint_count > endpoint_limit:
        logger.warning(
            f"接口限流触发: user_id={user_id}, endpoint={endpoint}, "
            f"count={endpoint_count}/{endpoint_limit}"
        )
        return False
    
    return True


def _get_endpoint_limit(endpoint: str) -> int:
    """
    根据端点路径获取对应的限流阈值
    
    使用最长前缀匹配：如 /api/v1/chat/stream 会匹配到 /api/v1/chat 的限流配置。
    
    Args:
        endpoint: 请求的 API 端点路径
        
    Returns:
        该端点的每分钟限流次数
    """
    # 最长前缀匹配
    best_match = None
    best_length = 0
    
    for prefix, limit in ENDPOINT_RATE_LIMITS.items():
        if endpoint.startswith(prefix) and len(prefix) > best_length:
            best_match = limit
            best_length = len(prefix)
    
    return best_match if best_match is not None else DEFAULT_RATE_LIMIT


def rate_limit(
    global_limit: int = GLOBAL_RATE_LIMIT,
    endpoint_limit: Optional[int] = None
):
    """
    限流依赖工厂函数
    
    返回一个 FastAPI 依赖函数，用于在路由中通过 Depends(rate_limit(...)) 注入。
    
    使用示例：
        @router.get("/chat")
        async def chat(user_id=Depends(get_current_user_id), _=Depends(rate_limit(endpoint_limit=10))):
            ...
    
    Args:
        global_limit: 全局每分钟限流次数
        endpoint_limit: 接口级每分钟限流次数
        
    Returns:
        FastAPI 依赖函数
    """
    
    async def dependency(request: Request):
        """
        限流依赖：从请求中获取用户 ID 和 Redis 连接，执行限流检查

        对于已认证用户，使用 user_id 作为限流标识；
        对于未认证请求（如登录接口），使用客户端 IP 作为限流标识，
        确保登录防暴力破解限流真正生效。
        """
        # 从 app.state 获取 Redis 连接
        redis_client: aioredis.Redis = request.app.state.redis

        # 优先使用已认证的 user_id，未认证时回退到客户端 IP
        user_id = getattr(request.state, "user_id", None)
        if user_id is None:
            # 未认证请求使用客户端 IP 作为标识（如登录接口）
            user_id = request.client.host if request.client else "unknown"
            # 为未认证用户添加前缀以区分
            user_id = f"anon:{user_id}"

        endpoint = request.url.path

        # 执行限流检查
        allowed = await check_rate_limit(
            redis_client=redis_client,
            user_id=user_id,
            endpoint=endpoint,
            global_limit=global_limit,
            endpoint_limit=endpoint_limit
        )

        if not allowed:
            raise BusinessError(
                code=ErrorCode.ENDPOINT_RATE_LIMIT, http_status=429,
                data={"reason": "ENDPOINT_RATE_LIMIT"},
            )

    return dependency
