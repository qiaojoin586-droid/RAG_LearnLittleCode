"""
MySQL 数据库配置模块

提供异步 SQLAlchemy 引擎和会话管理：
- 异步引擎（aiomysql）连接池：10 + 20
- per-request 会话依赖注入（get_db）
- 数据库初始化（init_db）：自动建表 + 补列迁移
- UTF-8 MB4 编码
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import inspect, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.logger_handler import logger
from app.models.base import Base

# ========== 数据库连接配置 ==========

# 从环境变量构建数据库 URL（使用 aiomysql 异步驱动）
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "raglearn")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "raglearn")

DATABASE_URL = (
    f"mysql+aiomysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
)

# ========== 异步引擎与会话工厂 ==========

# 异步引擎：连接池 20（最小）+ 30（最大溢出）
# 单个 /chat/query 请求最多打开 4-6 个独立 session，10 并发即可耗尽旧配置（pool=10+20）
# 扩容至 20+30=50，支撑约 15-20 并发用户（需 MySQL max_connections >= 200）
engine = create_async_engine(
    url=DATABASE_URL,
    pool_size=20,
    max_overflow=30,
    pool_pre_ping=True,       # 连接前 ping 检测，自动重连断开的连接
    pool_recycle=3600,        # 连接回收时间（秒），防止 MySQL 8 小时超时断连
    echo=False,               # 生产环境关闭 SQL 日志
)


@event.listens_for(engine.sync_engine, "connect")
def _set_mysql_session_utc(dbapi_connection, connection_record):
    """连接建立后强制 MySQL 会话时区为 UTC。

    func.now() 的返回值依赖会话时区：若服务器时区漂移（如换机器/容器时区变化），
    存储时间会跟着变。统一强制 UTC 后，存储语义稳定（与 time_utils 的 UTC 约定配套），
    前端展示时按浏览器本地时区自动转换（如北京时间 +08:00）。
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET time_zone = '+00:00'")
        cursor.close()
    except Exception:
        # 非 MySQL 驱动（如测试用 sqlite）无此语句，静默跳过
        pass

# 异步会话工厂
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,   # commit 后不自动过期，允许延迟访问属性
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI 依赖注入：提供 per-request 的异步数据库会话
    
    每个请求获取独立的 AsyncSession，请求结束后自动关闭。
    自动处理 commit 和 rollback：
    - 正常执行完毕自动 commit（如果路由未显式 commit）
    - 发生异常自动 rollback
    
    注意：如果路由已显式 commit，此处的 commit 是空操作（autobegin 新事务），
    不会影响已持久化的数据。commit 失败时仅记录日志，不传播异常，
    因为此时响应已构建，传播异常无意义且可能干扰已提交的数据。
    
    Yields:
        AsyncSession: 异步数据库会话
    """
    async with async_session_factory() as session:
        try:
            yield session
            try:
                await session.commit()
            except Exception as commit_err:
                # 路由可能已显式 commit（如批量操作），此处 commit 是 autobegin 空事务。
                # 如果空事务 commit 失败（连接断开等），已提交的数据不受影响。
                # 仅记录日志，不传播异常。
                logger.warning(f"get_db auto-commit 异常（已提交数据不受影响）: {commit_err}")
                try:
                    await session.rollback()
                except Exception:
                    pass
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """
    初始化数据库表结构（Alembic 迁移驱动，审查 P1-2 替代手写 create_all + 补列）

    流程：
    1. 遗留库检测：有业务表但无 alembic_version 表（create_all/_migrate_columns 时代
       创建的库），仅标记固定初始版本，后续迁移检查并补齐缺失的表、列与索引
    2. 执行 `alembic upgrade head`：空库自动全量建表，有版本则增量迁移

    Alembic 运行在线程中（asyncio.to_thread），内部自建独立引擎，
    避免跨 event loop 复用连接（见 alembic/env.py 说明）。
    """
    import asyncio

    from alembic import command
    from alembic.config import Config

    # alembic.ini 固定在项目根目录（支持任意 CWD 启动）
    alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    cfg = Config(str(alembic_ini))

    # 固定到初始基线，不能用 head：旧库未必已经具备新增分类、全文索引和文档类型。
    async with engine.connect() as conn:
        def _check_legacy(sync_conn):
            inspector = inspect(sync_conn)
            tables = set(inspector.get_table_names())
            return bool(tables) and "alembic_version" not in tables

        is_legacy = await conn.run_sync(_check_legacy)

    if is_legacy:
        logger.info("检测到遗留数据库（无 alembic_version），从初始基线补跑增量迁移")
        await asyncio.to_thread(command.stamp, cfg, "06f20a30109b")

    await asyncio.to_thread(command.upgrade, cfg, "head")
    logger.info("数据库迁移完成 (alembic upgrade head)")


async def close_db() -> None:
    """
    关闭数据库连接池
    
    在应用关闭时调用，释放所有数据库连接资源。
    """
    await engine.dispose()
    logger.info("数据库连接池已关闭")
