"""
用户认证路由

端点：
- POST /auth/register - 用户注册
- POST /auth/login - 用户登录
- POST /auth/logout - 用户登出
- POST /auth/refresh - Token 刷新
- POST /auth/sse-token - 获取 SSE 短期 Token
- GET /user/me - 获取当前用户信息
- PUT /user/me - 更新用户信息
- POST /user/me/password - 修改密码
- POST /file/avatar - 上传头像
"""

import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.success_response import success_response
from app.core.failed_response import BusinessError, ErrorCode
from app.core.rate_limit import rate_limit
from app.db.database import get_db
from app.models.user import User
from app.schemas.auth import (
    UserRegister, UserLogin, TokenResponse,
    RefreshTokenRequest, UserUpdate, PasswordChange, UserInfo, SessionInfo,
    SendCodeRequest, EmailChangeRequest,
)
from app.utils.auth_utils import (
    hash_password, verify_password, validate_password_strength,
    create_access_token, create_refresh_token, decode_token,
    get_current_user_id, check_login_attempts, record_login_failure,
    clear_login_attempts, store_refresh_token, verify_refresh_token,
    revoke_refresh_token, revoke_all_refresh_tokens, blacklist_access_token,
    store_device_session, get_device_session, update_device_session,
    delete_device_session, list_user_sessions, enforce_session_limit,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)
from app.db.redis_client import get_redis
from app.utils.file_handler import validate_avatar_file, get_safe_filename, ensure_dir, read_upload_limited

router = APIRouter()


def _get_email_service():
    """获取 EmailService 实例（优先复用 init_manager 中的单例，否则按环境变量新建）"""
    try:
        from main import init_manager
        if init_manager.email_service:
            return init_manager.email_service
    except Exception:
        pass
    from app.services.email_service import EmailService
    return EmailService()


@router.post("/auth/register", summary="用户注册", dependencies=[Depends(rate_limit(endpoint_limit=5))])
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    """
    注册新用户

    校验邮箱验证码、用户名唯一性、邮箱唯一性、密码强度，创建用户记录（email_verified=True）。
    """
    # 校验邮箱验证码（一次性，校验通过即删除 Redis 记录）
    email_service = _get_email_service()
    if not await email_service.verify_code(data.email, data.verification_code):
        raise BusinessError(code=ErrorCode.EMAIL_CODE_INVALID, http_status=400)

    # 检查邮箱是否已被占用（users.email 有 UNIQUE 约束，此处提前返回友好提示）
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise BusinessError(code=ErrorCode.EMAIL_EXISTS, http_status=409)

    # 检查用户名是否已存在
    result = await db.execute(select(User).where(User.username == data.username))
    if result.scalar_one_or_none():
        raise BusinessError(code=ErrorCode.USERNAME_EXISTS, http_status=409)

    # 校验密码强度
    is_valid, error_msg = validate_password_strength(data.password)
    if not is_valid:
        raise BusinessError(code=ErrorCode.INVALID_PARAMETER, message=error_msg)

    # 创建用户
    user = User(
        uuid=str(uuid.uuid4()),
        username=data.username,
        email=data.email,
        email_verified=True,
        password=hash_password(data.password),
    )
    db.add(user)
    await db.flush()

    # 为新用户创建模板分类树（与存量数据迁移共用模板，见 category_service.TEMPLATE_TREE）
    from app.services.category_service import seed_template_tree
    await seed_template_tree(db, user.uuid)

    logger.info(f"用户注册成功: username={data.username}, id={user.uuid}, email_verified=True")

    return success_response(data={"user_id": user.uuid, "username": user.username})


@router.post("/auth/send-code", summary="发送邮箱验证码", dependencies=[Depends(rate_limit(endpoint_limit=10))])
async def send_code(data: SendCodeRequest, request: Request):
    """
    发送邮箱验证码（注册 / 修改邮箱复用）

    限流策略：
    - 同一邮箱 60 秒内仅可发送 1 次（Redis key: email_code_cooldown:{email}）
    - 同一 IP 每小时最多 10 次（Redis key: send_code_ip:{ip}:{hour}，端点内手动实现）
    - 分钟级限流由 rate_limit 装饰器兜底（/api/v1/auth/send-code: 10 次/分钟）
    """
    redis = get_redis()

    # 1. 邮箱冷却检查（60s）
    cooldown_key = f"email_code_cooldown:{data.email}"
    if await redis.exists(cooldown_key):
        ttl = await redis.ttl(cooldown_key)
        raise BusinessError(
            code=ErrorCode.ENDPOINT_RATE_LIMIT,
            message=f"发送过于频繁，请 {ttl} 秒后再试",
            http_status=429,
        )

    # 2. IP 小时级限流（10 次/小时）
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.headers.get("X-Real-IP", "").strip()
    )
    if not client_ip:
        client_ip = request.client.host if request.client else None
    if not client_ip:
        # 无法获取 IP → 跳过 IP 限流，仅依赖邮箱维度限流
        logger.warning("send-code: 无法获取客户端 IP，跳过 IP 限流")
    else:
        hour_bucket = datetime.now().strftime("%Y%m%d%H")
        ip_key = f"send_code_ip:{client_ip}:{hour_bucket}"
        ip_count = await redis.incr(ip_key)
        if ip_count == 1:
            await redis.expire(ip_key, 3600)
        if ip_count > 10:
            raise BusinessError(
                code=ErrorCode.ENDPOINT_RATE_LIMIT,
                message="请求过于频繁，请稍后再试",
                http_status=429,
            )

    # 3. 生成验证码 + 发送邮件（失败返回友好提示，不让用户看到 SMTP 异常堆栈）
    email_service = _get_email_service()
    if not email_service.available:
        logger.error("send-code: SMTP 未配置，无法发送邮件")
        raise BusinessError(code=ErrorCode.EMAIL_SEND_FAILED, http_status=503)

    try:
        await email_service.send_verification_code(data.email)
    except Exception as e:
        logger.error(f"send-code: 邮件发送失败 email={data.email}, error={e}")
        raise BusinessError(code=ErrorCode.EMAIL_SEND_FAILED, http_status=502)

    # 4. 设置发送冷却（60s）
    await redis.set(cooldown_key, "1", ex=60)

    logger.info(f"验证码已发送: email={data.email}")
    return success_response(message="验证码已发送")


@router.post("/user/change-email", summary="修改/绑定邮箱")
async def change_email(
    data: EmailChangeRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    修改/绑定邮箱（两步流程第二步）

    流程：
    1. 校验验证码（复用 EmailService.verify_code，一次性）
    2. 检查新邮箱是否已被其他用户占用
    3. 更新 user.email + email_verified=True
    """
    # 1. 校验验证码
    email_service = _get_email_service()
    if not await email_service.verify_code(data.email, data.verification_code):
        raise BusinessError(code=ErrorCode.EMAIL_CODE_INVALID, http_status=400)

    # 2. 检查新邮箱是否被其他用户占用（排除自己）
    result = await db.execute(select(User).where(User.email == data.email))
    existing = result.scalar_one_or_none()
    if existing and existing.uuid != user_id:
        raise BusinessError(code=ErrorCode.EMAIL_EXISTS, http_status=409)

    # 3. 更新邮箱
    result = await db.execute(select(User).where(User.uuid == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise BusinessError(code=ErrorCode.USER_NOT_FOUND, http_status=404)

    user.email = data.email
    user.email_verified = True
    await db.flush()

    logger.info(f"邮箱修改成功: user_id={user_id}, new_email={data.email}")
    return success_response(message="邮箱修改成功")


@router.post("/auth/login", summary="用户登录", dependencies=[Depends(rate_limit(endpoint_limit=5))])
async def login(data: UserLogin, request: Request, db: AsyncSession = Depends(get_db)):
    """
    用户登录
    
    校验用户名密码，返回 Access Token + Refresh Token。
    连续 5 次失败锁定 15 分钟。
    支持设备会话复用：同一设备重复登录时旧 refresh token 立即失效。
    """
    # 检查是否被锁定
    await check_login_attempts(data.username)
    
    # 查找用户
    result = await db.execute(select(User).where(User.username == data.username))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(data.password, user.password):
        await record_login_failure(data.username)
        raise BusinessError(code=ErrorCode.PASSWORD_ERROR, http_status=401)
    
    # 登录成功，清除失败计数
    await clear_login_attempts(data.username)
    
    # === 新增：设备会话复用 ===
    if data.device_id:
        existing = await get_device_session(user.uuid, data.device_id)
        if existing:
            old_jti = existing.get("jti")
            if old_jti and await verify_refresh_token(user.uuid, old_jti):
                # 同一设备重复登录 → 撤销旧 refresh_token (rotation)
                await revoke_refresh_token(user.uuid, old_jti)
    
    # 生成令牌对
    access_token = create_access_token(user.uuid)
    refresh_token, jti = create_refresh_token(user.uuid)
    
    # 存储 Refresh Token 白名单
    await store_refresh_token(user.uuid, jti)
    
    # === 新增：存储/更新设备会话 ===
    if data.device_id:
        await store_device_session(
            user.uuid, data.device_id, jti,
            device_name=data.device_name,
            request=request,
        )
        await enforce_session_limit(user.uuid, max_sessions=5)
    
    logger.info(f"用户登录成功: username={data.username}, device_id={data.device_id or 'N/A'}")
    
    return success_response(data=TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        device_id=data.device_id,
    ).model_dump())


@router.post("/auth/logout", summary="用户登出")
async def logout(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    用户登出
    
    将 Access Token 加入黑名单，删除 Refresh Token 白名单。
    如果提供了 device_id，只撤销当前设备会话；否则撤销所有。
    """
    # 从 Request headers 获取 Authorization（不再用 Depends lambda）
    authorization = request.headers.get("authorization", "")
    
    # 解析当前 Token 的 jti
    token = authorization.split()[-1] if authorization else ""
    jti = None
    if token:
        try:
            payload = decode_token(token)
            jti = payload.get("jti")
        except Exception:
            pass
    
    if jti:
        # 计算剩余有效期，加入黑名单
        from datetime import datetime, timezone
        exp = payload.get("exp", 0)
        remaining = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
        await blacklist_access_token(jti, remaining)
    
    # 尝试从 body 获取 device_id（可选，前端可能不传 body）
    device_id = None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            device_id = body.get("device_id")
        except Exception:
            pass
    
    if device_id:
        # 只撤销当前设备会话
        await delete_device_session(user_id, device_id)
        # 撤销该设备关联的 refresh token
        if jti:
            await revoke_refresh_token(user_id, jti)
    else:
        # 向后兼容：撤销所有 refresh token + 清理所有设备会话
        await revoke_all_refresh_tokens(user_id)
    
    logger.info(f"用户登出: user_id={user_id}, device_id={device_id or 'ALL'}, token_blacklisted={jti is not None}")
    return success_response(message="登出成功")


@router.post("/auth/refresh", summary="刷新 Token")
async def refresh_token(data: RefreshTokenRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    刷新 Token（Rotation 防重放）
    
    校验 Refresh Token → 签发新令牌对 → 旧 Refresh Token 立即失效。
    如果提供了 device_id，同步更新设备会话。
    """
    # 解码 Refresh Token
    payload = decode_token(data.refresh_token)
    
    if payload.get("type") != "refresh":
        raise BusinessError(code=ErrorCode.REFRESH_TOKEN_INVALID, http_status=401)
    
    user_id = payload.get("sub")
    jti = payload.get("jti")
    
    # 验证白名单
    if not await verify_refresh_token(user_id, jti):
        raise BusinessError(code=ErrorCode.REFRESH_TOKEN_INVALID, http_status=401)
    
    # 旧 Token 立即失效（Rotation）
    await revoke_refresh_token(user_id, jti)
    
    # 签发新令牌对
    new_access = create_access_token(user_id)
    new_refresh, new_jti = create_refresh_token(user_id)
    await store_refresh_token(user_id, new_jti)
    
    # === 新增：更新设备会话 ===
    if data.device_id:
        await update_device_session(user_id, data.device_id, new_jti, request)
    
    return success_response(data=TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        device_id=data.device_id,
    ).model_dump())


@router.post("/auth/sse-token", summary="获取 SSE 短期 Token")
async def get_sse_token(user_id: str = Depends(get_current_user_id)):
    """
    用有效 Access Token 换取 60 秒一次性 SSE Token
    
    SSE 连接无法携带自定义 Header，使用短期 Query Token 替代。
    """
    import uuid as _uuid
    redis = get_redis()
    
    sse_jti = str(_uuid.uuid4())
    sse_token = create_access_token(user_id, extra_claims={"type": "sse", "jti": sse_jti})
    
    # 存入 Redis，60 秒过期，一次性使用
    await redis.setex(f"sse_token:{sse_jti}", 60, user_id)
    
    return success_response(data={"token": sse_token, "expires_in": 60})


# ========== 设备会话管理 ==========


@router.get("/auth/sessions", summary="查看活跃会话列表")
async def get_sessions(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    获取当前用户的所有活跃会话
    
    返回各设备名称、IP、活跃时间等信息。
    """
    # 获取当前请求的 device_id（用于标记 is_current）
    current_device_id = request.headers.get("X-Device-Id")
    
    sessions = await list_user_sessions(user_id, current_device_id)
    
    return success_response(data={"sessions": sessions})


@router.delete("/auth/sessions/{device_id}", summary="撤销设备会话")
async def revoke_session(
    device_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    主动撤销指定设备的会话
    
    撤销后该设备的 refresh token 立即失效。
    如果是当前设备，同时 blacklist 当前 access_token。
    """
    # 查找该设备的会话信息
    session = await get_device_session(user_id, device_id)
    if not session:
        raise BusinessError(code=ErrorCode.DEVICE_SESSION_NOT_FOUND, http_status=404)
    
    # 撤销关联的 refresh token
    old_jti = session.get("jti")
    if old_jti:
        await revoke_refresh_token(user_id, old_jti)
    
    # 删除设备会话记录
    await delete_device_session(user_id, device_id)
    
    # 检查是否是当前设备（通过 X-Device-Id 请求头判断）
    current_device_id = request.headers.get("X-Device-Id")
    if current_device_id == device_id:
        # 同时 blacklist 当前 access_token
        authorization = request.headers.get("authorization", "")
        token = authorization.split()[-1] if authorization else ""
        if token:
            try:
                payload = decode_token(token)
                jti = payload.get("jti")
                if jti:
                    from datetime import datetime, timezone
                    exp = payload.get("exp", 0)
                    remaining = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
                    await blacklist_access_token(jti, remaining)
            except Exception:
                pass
        logger.info(f"当前设备会话已撤销: device_id={device_id}")
        return success_response(message="当前会话已注销")
    
    logger.info(f"设备会话已撤销: user_id={user_id}, device_id={device_id}")
    return success_response(message="会话已撤销")


@router.get("/user/me", summary="获取当前用户信息")
async def get_user_info(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取当前登录用户的详细信息"""
    result = await db.execute(select(User).where(User.uuid == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise BusinessError(code=ErrorCode.USER_NOT_FOUND, http_status=404)
    
    return success_response(data=UserInfo.model_validate(user).model_dump())


@router.put("/user/me", summary="更新用户信息")
async def update_user_info(
    data: UserUpdate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户的个人资料"""
    # 兜底校验：邮箱变更必须走 POST /user/change-email 验证流程
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if "email" in body:
            raise BusinessError(
                code=ErrorCode.INVALID_PARAMETER,
                message="邮箱修改需通过验证码验证，请使用'修改邮箱'功能",
            )

    result = await db.execute(select(User).where(User.uuid == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise BusinessError(code=ErrorCode.USER_NOT_FOUND, http_status=404)

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    await db.flush()
    return success_response(message="更新成功")


@router.post("/user/me/password", summary="修改密码")
async def change_password(
    data: PasswordChange,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    修改密码
    
    需提供旧密码验证，修改后所有 Refresh Token 立即失效。
    """
    result = await db.execute(select(User).where(User.uuid == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise BusinessError(code=ErrorCode.USER_NOT_FOUND, http_status=404)
    
    if not verify_password(data.old_password, user.password):
        raise BusinessError(code=ErrorCode.PASSWORD_ERROR, http_status=403)
    
    # 校验新密码强度
    is_valid, error_msg = validate_password_strength(data.new_password)
    if not is_valid:
        raise BusinessError(code=ErrorCode.INVALID_PARAMETER, message=error_msg)
    
    user.password = hash_password(data.new_password)
    await db.flush()
    
    # 所有 Refresh Token 失效
    await revoke_all_refresh_tokens(user_id)
    
    logger.info(f"密码修改成功: user_id={user_id}")
    return success_response(message="密码修改成功")


# ========== 头像上传 ==========

# 头像存储目录（相对于项目根目录）
AVATAR_DIR = "data/avatars"


@router.post("/file/avatar", summary="上传头像")
async def upload_avatar(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    上传用户头像

    支持 PNG / JPG / WebP 格式，单文件上限 5MB。
    上传成功后自动更新用户 avatar 字段，返回可访问的头像 URL。
    """
    # 读取文件内容（分块限流，超限立即中断，防内存 DoS；头像上限 5MB）
    content = await read_upload_limited(file, max_size_mb=5)
    file_size = len(content)

    # 校验头像文件（格式 + 大小）
    ext = validate_avatar_file(file.filename, file_size)

    # 保存文件（以 user_id 分目录，文件名用时间戳避免冲突）
    avatar_dir = Path(AVATAR_DIR) / user_id
    ensure_dir(str(avatar_dir))

    timestamp = int(datetime.now().timestamp() * 1000)
    stored_name = f"avatar_{timestamp}{ext}"
    file_path = avatar_dir / stored_name

    with open(file_path, "wb") as f:
        f.write(content)

    # 删除旧头像文件（避免磁盘垃圾）
    result = await db.execute(select(User).where(User.uuid == user_id))
    user = result.scalar_one_or_none()
    if user and user.avatar:
        old_path = Path(user.avatar.lstrip("/"))
        if old_path.exists():
            try:
                old_path.unlink()
            except Exception as e:
                logger.warning(f"删除旧头像失败: {e}")

    # 更新用户头像 URL（静态文件服务路径）
    avatar_url = f"/static/avatars/{user_id}/{stored_name}"
    if user:
        user.avatar = avatar_url
        await db.flush()

    logger.info(f"头像上传成功: user_id={user_id}, url={avatar_url}")

    return success_response(data={
        "avatar_url": avatar_url,
        "filename": stored_name,
    })
