"""
认证工具模块

提供 JWT 认证相关功能：
- JWT Token 编解码（Access Token + Refresh Token）
- 密码哈希（bcrypt）
- FastAPI 依赖注入：get_current_user_id 从请求中提取当前用户 ID
- 登录失败计数与账户锁定（Redis）
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, Header, HTTPException, Request
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, ErrorCode
from app.db.database import get_db
from app.db.redis_client import get_redis
from app.models.user import User

# ========== 配置 ==========

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")

# 已知的公开默认密钥：生产环境漏配时直接拒绝启动，
# 避免攻击者使用公开密钥伪造任意用户的 token
_INSECURE_JWT_SECRETS = {
    "change-me-in-production",
    "raglearn-dev-secret-key-change-in-production-2026",
}


def validate_jwt_secret() -> None:
    """
    校验 JWT_SECRET 是否安全（应用启动阶段调用）

    未配置、使用公开默认值或过短的密钥时抛出 RuntimeError 拒绝启动。
    生成强随机密钥示例：
        python -c "import secrets; print(secrets.token_hex(32))"
    """
    if not JWT_SECRET or JWT_SECRET in _INSECURE_JWT_SECRETS or len(JWT_SECRET) < 16:
        raise RuntimeError(
            "JWT_SECRET 未配置或使用公开默认值，拒绝启动。"
            "请在 .env 中配置强随机密钥（至少 16 字符），例如："
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )


JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# 密码哈希上下文（bcrypt）
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 登录锁定配置
MAX_LOGIN_ATTEMPTS = 5          # 最大失败次数
LOCKOUT_DURATION = 900          # 锁定时长（秒）= 15 分钟


# ========== 密码哈希 ==========

def hash_password(password: str) -> str:
    """
    对明文密码进行 bcrypt 哈希
    
    Args:
        password: 明文密码
        
    Returns:
        bcrypt 哈希后的密码字符串
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证明文密码是否与哈希值匹配
    
    Args:
        plain_password: 明文密码
        hashed_password: 存储的 bcrypt 哈希值
        
    Returns:
        匹配返回 True，否则 False
    """
    return pwd_context.verify(plain_password, hashed_password)


def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    验证密码强度
    
    要求：
    - 最小长度 8 位
    - 至少包含字母 + 数字
    
    Args:
        password: 待验证的密码
        
    Returns:
        (是否通过, 错误消息)
    """
    if len(password) < 8:
        return False, "密码长度至少 8 位"
    if not any(c.isalpha() for c in password):
        return False, "密码必须包含字母"
    if not any(c.isdigit() for c in password):
        return False, "密码必须包含数字"
    return True, ""


# ========== JWT Token ==========

def create_access_token(user_id: str, extra_claims: Optional[dict] = None) -> str:
    """
    创建 Access Token（短生命周期）
    
    Args:
        user_id: 用户 UUID
        extra_claims: 额外的 JWT payload 字段
        
    Returns:
        编码后的 JWT 字符串
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "exp": expire,
        "jti": str(uuid.uuid4()),      # 唯一标识，用于黑名单
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> tuple[str, str]:
    """
    创建 Refresh Token（长生命周期）
    
    同时在 Redis 白名单中记录该 Token，用于续期时校验。
    
    Args:
        user_id: 用户 UUID
        
    Returns:
        (token_string, jti): 编码后的 JWT 和唯一标识
    """
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    
    payload = {
        "sub": user_id,
        "exp": expire,
        "jti": jti,
        "type": "refresh",
    }
    
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, jti


def decode_token(token: str) -> dict:
    """
    解码并验证 JWT Token
    
    Args:
        token: JWT 字符串
        
    Returns:
        解码后的 payload 字典
        
    Raises:
        BusinessError: Token 无效或已过期
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as e:
        error_msg = str(e)
        if "expired" in error_msg.lower():
            raise BusinessError(
                code=ErrorCode.TOKEN_EXPIRED,
                detail=error_msg,
                http_status=401
            )
        raise BusinessError(
            code=ErrorCode.TOKEN_INVALID,
            detail=error_msg,
            http_status=401
        )


# ========== 登录锁定 ==========

async def check_login_attempts(username: str) -> None:
    """
    检查用户是否被锁定（登录失败次数过多）
    
    Args:
        username: 用户名
        
    Raises:
        BusinessError: 账户已锁定
    """
    redis = get_redis()
    attempts = await redis.get(f"login_attempts:{username}")
    if attempts and int(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise BusinessError(
            code=ErrorCode.ACCOUNT_LOCKED,
            http_status=403
        )


async def record_login_failure(username: str) -> None:
    """
    记录登录失败次数
    
    Args:
        username: 用户名
    """
    redis = get_redis()
    key = f"login_attempts:{username}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, LOCKOUT_DURATION)
    
    logger.warning(f"登录失败: username={username}, attempts={count}")


async def clear_login_attempts(username: str) -> None:
    """
    登录成功后清除失败计数
    
    Args:
        username: 用户名
    """
    redis = get_redis()
    await redis.delete(f"login_attempts:{username}")


# ========== Refresh Token 管理 ==========

async def store_refresh_token(user_id: str, jti: str) -> None:
    """
    将 Refresh Token 存入 Redis 白名单
    
    Args:
        user_id: 用户 UUID
        jti: Token 唯一标识
    """
    redis = get_redis()
    key = f"refresh_token:{user_id}:{jti}"
    ttl = REFRESH_TOKEN_EXPIRE_DAYS * 86400  # 转为秒
    await redis.setex(key, ttl, "1")


async def verify_refresh_token(user_id: str, jti: str) -> bool:
    """
    验证 Refresh Token 是否在白名单中
    
    Args:
        user_id: 用户 UUID
        jti: Token 唯一标识
        
    Returns:
        有效返回 True
    """
    redis = get_redis()
    key = f"refresh_token:{user_id}:{jti}"
    return bool(await redis.get(key))


async def revoke_refresh_token(user_id: str, jti: str) -> None:
    """
    撤销 Refresh Token（从白名单中删除）
    
    同时查找并删除关联的设备会话记录。
    
    Args:
        user_id: 用户 UUID
        jti: Token 唯一标识
    """
    redis = get_redis()
    await redis.delete(f"refresh_token:{user_id}:{jti}")
    
    # 同时清理关联的设备会话（扫描所有设备会话查找匹配的 jti）
    try:
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"session:{user_id}:*", count=100)
            for key in keys:
                session_jti = await redis.hget(key, "jti")
                if session_jti == jti:
                    device_id = key.split(":")[-1]
                    await redis.delete(key)
                    await redis.srem(f"user_sessions:{user_id}", device_id)
                    logger.debug(f"设备会话已清理: device_id={device_id}")
                    break
            if cursor == 0:
                break
    except Exception as e:
        logger.warning(f"清理设备会话失败: {e}")


async def revoke_all_refresh_tokens(user_id: str) -> None:
    """
    撤销用户所有 Refresh Token（如修改密码后）
    
    同时删除所有设备会话记录。
    
    Args:
        user_id: 用户 UUID
    """
    redis = get_redis()
    
    # 删除所有 refresh token
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"refresh_token:{user_id}:*", count=100)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break
    
    # 删除所有设备会话
    try:
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"session:{user_id}:*", count=100)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break
        await redis.delete(f"user_sessions:{user_id}")
    except Exception as e:
        logger.warning(f"清理设备会话失败: {e}")


# ========== Access Token 黑名单 ==========

async def blacklist_access_token(jti: str, remaining_ttl: int) -> None:
    """
    将 Access Token 加入黑名单（登出时）
    
    Args:
        jti: Token 唯一标识
        remaining_ttl: Token 剩余有效期（秒）
    """
    redis = get_redis()
    key = f"token_blacklist:{jti}"
    await redis.setex(key, remaining_ttl, "1")


async def is_token_blacklisted(jti: str) -> bool:
    """
    检查 Access Token 是否在黑名单中
    
    Args:
        jti: Token 唯一标识
        
    Returns:
        在黑名单返回 True
    """
    redis = get_redis()
    return bool(await redis.get(f"token_blacklist:{jti}"))


# ========== FastAPI 依赖注入 ==========

async def get_current_user_id(
    request: Request,                                   # ★ C2：供限流按 user_id 计数
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
) -> str:
    """
    FastAPI 依赖注入：从 Authorization Header 中提取当前用户 ID
    
    流程：
    1. 解析 Bearer Token
    2. 解码 JWT
    3. 检查是否在黑名单中
    4. 返回 user_id
    
    Args:
        authorization: Authorization 请求头
        db: 数据库会话
        
    Returns:
        当前用户的 UUID
        
    Raises:
        BusinessError: 认证失败
    """
    if not authorization:
        raise BusinessError(
            code=ErrorCode.TOKEN_INVALID,
            message="缺少认证信息",
            http_status=401
        )
    
    # 解析 Bearer Token
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise BusinessError(
            code=ErrorCode.TOKEN_INVALID,
            message="Authorization 格式错误，应为 'Bearer <token>'",
            http_status=401
        )
    
    token = parts[1]
    
    # 解码 JWT
    payload = decode_token(token)
    
    # 验证类型
    if payload.get("type") != "access":
        raise BusinessError(
            code=ErrorCode.TOKEN_INVALID,
            message="Token 类型错误",
            http_status=401
        )
    
    # 检查黑名单
    jti = payload.get("jti")
    if jti and await is_token_blacklisted(jti):
        raise BusinessError(
            code=ErrorCode.TOKEN_INVALID,
            message="Token 已被撤销",
            http_status=401
        )
    
    user_id = payload.get("sub")
    if not user_id:
        raise BusinessError(
            code=ErrorCode.TOKEN_INVALID,
            http_status=401
        )

    # ★ S3/C2：写入 request.state，供限流依赖（rate_limit）按 user_id 计数。
    # 生效前提：端点参数里 get_current_user_id 必须排在 rate_limit 之前
    #（router 级 dependencies 会先执行，读不到这里写入的值）。
    request.state.user_id = user_id

    return user_id


# ========== 设备会话管理 ==========

async def store_device_session(
    user_id: str,
    device_id: str,
    jti: str,
    device_name: Optional[str] = None,
    request: Optional[Request] = None
) -> None:
    """
    创建或覆盖设备会话记录
    
    写入 session:{user_id}:{device_id} Hash，并将 device_id 加入
    user_sessions:{user_id} Set。TTL 与 refresh token 一致。
    
    Args:
        user_id: 用户 UUID
        device_id: 设备唯一标识
        jti: 关联的 Refresh Token JTI
        device_name: 前端提供的设备可读名称
        request: FastAPI Request（用于提取 IP 和 User-Agent）
    """
    redis = get_redis()
    now = datetime.now(timezone.utc).isoformat()
    
    # 提取 IP 和 User-Agent
    ip = ""
    user_agent = ""
    if request:
        ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
    
    # 如果前端没提供 device_name，从 UA 解析
    if not device_name and user_agent:
        device_name = parse_device_name(user_agent)
    
    session_key = f"session:{user_id}:{device_id}"
    ttl = REFRESH_TOKEN_EXPIRE_DAYS * 86400
    
    # 检查是否已存在会话（保留 created_at）
    existing = await redis.hget(session_key, "created_at")
    created_at = existing if existing else now
    
    # 写入会话信息（逐字段设置，兼容所有 redis-py 版本）
    session_data = {
        "jti": jti,
        "device_name": device_name or "Unknown Device",
        "ip": ip,
        "user_agent": user_agent,
        "created_at": created_at,
        "last_used": now,
    }
    for field, value in session_data.items():
        await redis.hset(session_key, field, value)
    await redis.expire(session_key, ttl)
    
    # 加入用户会话集合
    sessions_set_key = f"user_sessions:{user_id}"
    await redis.sadd(sessions_set_key, device_id)
    await redis.expire(sessions_set_key, ttl)
    
    logger.info(f"设备会话已存储: user_id={user_id}, device_id={device_id}")


async def get_device_session(
    user_id: str,
    device_id: str
) -> Optional[dict]:
    """
    获取设备会话信息
    
    Args:
        user_id: 用户 UUID
        device_id: 设备唯一标识
        
    Returns:
        会话 dict（含 jti, device_name, ip, created_at, last_used），不存在返回 None
    """
    redis = get_redis()
    session_key = f"session:{user_id}:{device_id}"
    data = await redis.hgetall(session_key)
    
    if not data:
        return None
    
    return {
        "jti": data.get("jti", ""),
        "device_name": data.get("device_name", ""),
        "ip": data.get("ip", ""),
        "user_agent": data.get("user_agent", ""),
        "created_at": data.get("created_at", ""),
        "last_used": data.get("last_used", ""),
    }


async def update_device_session(
    user_id: str,
    device_id: str,
    new_jti: str,
    request: Optional[Request] = None
) -> None:
    """
    更新设备会话元数据（token 轮换后调用）
    
    更新 jti、last_used、ip，并刷新 TTL。
    
    Args:
        user_id: 用户 UUID
        device_id: 设备唯一标识
        new_jti: 新的 Refresh Token JTI
        request: FastAPI Request
    """
    redis = get_redis()
    now = datetime.now(timezone.utc).isoformat()
    session_key = f"session:{user_id}:{device_id}"
    ttl = REFRESH_TOKEN_EXPIRE_DAYS * 86400
    
    # 更新字段
    await redis.hset(session_key, "jti", new_jti)
    await redis.hset(session_key, "last_used", now)
    if request and request.client:
        await redis.hset(session_key, "ip", request.client.host)
    await redis.expire(session_key, ttl)
    
    # 刷新集合 TTL
    await redis.expire(f"user_sessions:{user_id}", ttl)


async def delete_device_session(
    user_id: str,
    device_id: str
) -> None:
    """
    删除设备会话记录
    
    Args:
        user_id: 用户 UUID
        device_id: 设备唯一标识
    """
    redis = get_redis()
    await redis.delete(f"session:{user_id}:{device_id}")
    await redis.srem(f"user_sessions:{user_id}", device_id)
    logger.info(f"设备会话已删除: user_id={user_id}, device_id={device_id}")


async def list_user_sessions(
    user_id: str,
    current_device_id: Optional[str] = None
) -> List[dict]:
    """
    列出用户所有活跃会话
    
    Args:
        user_id: 用户 UUID
        current_device_id: 当前请求的设备 ID（用于标记 is_current）
        
    Returns:
        会话信息列表，按 last_used 倒序排列
    """
    redis = get_redis()
    sessions_set_key = f"user_sessions:{user_id}"
    
    # 获取所有 device_id
    device_ids = await redis.smembers(sessions_set_key)
    if not device_ids:
        return []
    
    sessions = []
    for device_id in device_ids:
        if isinstance(device_id, bytes):
            device_id = device_id.decode("utf-8")
        
        session_key = f"session:{user_id}:{device_id}"
        data = await redis.hgetall(session_key)
        
        if data:  # 会话存在（可能已部分过期）
            sessions.append({
                "device_id": device_id,
                "device_name": data.get("device_name", "Unknown Device"),
                "ip": data.get("ip", ""),
                "created_at": data.get("created_at", ""),
                "last_used": data.get("last_used", ""),
                "is_current": device_id == current_device_id if current_device_id else False,
            })
        else:
            # 会话 Hash 已过期但 Set 中还有，清理
            await redis.srem(sessions_set_key, device_id)
    
    # 按 last_used 倒序排列
    sessions.sort(key=lambda x: x.get("last_used", ""), reverse=True)
    return sessions


async def enforce_session_limit(
    user_id: str,
    max_sessions: int = 5
) -> None:
    """
    强制执行会话数量限制
    
    当 user_sessions Set 大小超过 max_sessions 时，
    按 created_at 排序，删除最旧的会话（包括对应的 refresh token）。
    
    Args:
        user_id: 用户 UUID
        max_sessions: 最大会话数
    """
    redis = get_redis()
    sessions_set_key = f"user_sessions:{user_id}"
    
    device_ids = await redis.smembers(sessions_set_key)
    if not device_ids or len(device_ids) <= max_sessions:
        return
    
    # 获取所有会话的 created_at
    sessions_with_time = []
    for device_id in device_ids:
        if isinstance(device_id, bytes):
            device_id = device_id.decode("utf-8")
        session_key = f"session:{user_id}:{device_id}"
        created_at = await redis.hget(session_key, "created_at")
        sessions_with_time.append((device_id, created_at or ""))
    
    # 按 created_at 升序排列（最旧的在前）
    sessions_with_time.sort(key=lambda x: x[1])
    
    # 删除最旧的会话，直到数量不超过限制
    to_remove = len(sessions_with_time) - max_sessions
    for i in range(to_remove):
        device_id = sessions_with_time[i][0]
        # 获取关联的 jti 并撤销 refresh token
        session_key = f"session:{user_id}:{device_id}"
        old_jti = await redis.hget(session_key, "jti")
        if old_jti:
            await redis.delete(f"refresh_token:{user_id}:{old_jti}")
        # 删除会话
        await redis.delete(session_key)
        await redis.srem(sessions_set_key, device_id)
        logger.info(f"会话数量超限，已删除最旧会话: device_id={device_id}")


def parse_device_name(user_agent: str) -> str:
    """
    从 User-Agent 字符串解析可读的设备名称
    
    不引入额外依赖，使用正则匹配常见浏览器和操作系统。
    
    Args:
        user_agent: User-Agent 请求头
        
    Returns:
        格式化的设备名，如 "Chrome / Windows"、"Safari / iOS"
    """
    if not user_agent:
        return "Unknown Device"
    
    # 操作系统检测
    os_name = "Unknown"
    if "Windows" in user_agent:
        os_name = "Windows"
    elif "Mac OS" in user_agent:
        os_name = "macOS"
    elif "iPhone" in user_agent or "iPad" in user_agent:
        os_name = "iOS"
    elif "Android" in user_agent:
        os_name = "Android"
    elif "Linux" in user_agent:
        os_name = "Linux"
    
    # 浏览器检测
    browser = "Unknown"
    if "Edg" in user_agent:
        browser = "Edge"
    elif "Chrome" in user_agent and "Safari" in user_agent:
        browser = "Chrome"
    elif "Safari" in user_agent:
        browser = "Safari"
    elif "Firefox" in user_agent:
        browser = "Firefox"
    
    return f"{browser} / {os_name}"


def extract_request_info(request: Request) -> tuple:
    """
    从 FastAPI Request 中提取 IP 和 User-Agent
    
    Args:
        request: FastAPI Request 对象
        
    Returns:
        (ip_address, user_agent) 元组
    """
    ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")
    return ip, user_agent
