"""
RAG LearnLittle — 智能笔记助手 · 应用入口

FastAPI 应用的生命周期管理：
- 启动阶段：加载环境变量、初始化数据库、连接 Redis、后台初始化模型
- 中间件：CORS、请求处理时间、全局异常处理
- 关闭阶段：释放数据库和 Redis 连接池
"""

import os
import time
import asyncio
from contextlib import asynccontextmanager

# 必须在所有 app 导入之前加载 .env，否则模块级 os.getenv 会读到默认值
from dotenv import load_dotenv
load_dotenv()   #加载环境变量

# 强制 HuggingFace 离线模式（阻止在线下载模型，仅使用本地缓存）
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.logger_handler import logger
from app.core.failed_response_register import register_exception_handlers
from app.core.metrics import MetricsMiddleware
from app.core.model_trace import start_trace_bus, stop_trace_bus
from app.db.database import init_db, close_db
from app.db.redis_client import init_redis, close_redis


# ========== 后台初始化管理器 ==========

class BackgroundInitManager:
    """
    后台初始化管理器
    
    在 FastAPI 启动后通过 asyncio.create_task 在后台异步初始化重型资源。
    分三个阶段顺序执行：
    1. AI 模型（Chat + Embedding + Vision）
    2. NoteService + ChromaDB
    3. 重排序模型
    
    每个阶段完成后设置 asyncio.Event，路由层通过 event.wait() 阻塞等待。
    """
    
    def __init__(self):
        """初始化阶段事件"""
        self.stage1_complete = asyncio.Event()  # AI 模型就绪
        self.stage2_complete = asyncio.Event()  # NoteService + ChromaDB 就绪
        self.stage3_complete = asyncio.Event()  # 重排序模型就绪
        
        # 管理的实例
        self.chat_model = None          # 默认对话模型（思考模式关闭，更快）
        self.chat_model_thinking = None # 深度思考模型（思考模式开启，前端开关控制）
        self.embed_model = None
        self.vision_model = None
        self.autocomplete_model = None  # AI 联机补全模型（轻量独立实例）
        self.note_service = None
        self.reorder_service = None
        self.rag_service = None  # RAG 服务（阶段 3 后创建）
        # EmailService：仅读取环境变量，不涉及异步 I/O 或重型资源，
        # 在 __init__ 中即可初始化（失败仅告警，邮件功能不可用）
        try:
            from app.services.email_service import EmailService
            self.email_service = EmailService()
        except Exception as e:
            logger.warning(f"EmailService 初始化失败（邮件功能不可用）: {e}")
            self.email_service = None

        # PptTemplateService + PptService：轻量对象（只读配置 + 目录初始化，
        # 不持有模型，§6.4/§6.5），在 __init__ 中同步创建（失败仅告警，PPT 功能不可用）
        try:
            from app.services.ppt_service import PptService, load_ppt_config
            from app.services.ppt_template_service import PptTemplateService
            self.ppt_template_service = PptTemplateService(load_ppt_config())
            self.ppt_service = PptService(load_ppt_config(), self.ppt_template_service)
        except Exception as e:
            logger.warning(f"PptService 初始化失败（PPT 功能不可用）: {e}")
            self.ppt_service = None
            self.ppt_template_service = None

        # MCP 客户端：无状态对象（配置 + 已注册工具），__init__ 中引用即可；
        # 真正的子进程拉起在 init_mcp()（独立后台任务）中执行
        self.mcp_manager = None

        # 后台初始化失败原因（None=成功或尚未失败，供 /ready 探针展示）
        self.init_error = None

    async def run(self):
        """
        执行后台初始化（三个阶段）
        """
        try:
            # 阶段 1: 初始化 AI 模型
            logger.info("后台初始化 - 阶段 1: AI 模型...")
            await self._init_models()
            self.stage1_complete.set()
            logger.info("后台初始化 - 阶段 1 完成 ✓")
            
            # 阶段 2: 初始化 NoteService + ChromaDB
            logger.info("后台初始化 - 阶段 2: NoteService + ChromaDB...")
            await self._init_services()
            self.stage2_complete.set()
            logger.info("后台初始化 - 阶段 2 完成 ✓")
            
            # 阶段 3: 初始化重排序模型
            logger.info("后台初始化 - 阶段 3: 重排序模型...")
            await self._init_reranker()
            self.stage3_complete.set()
            logger.info("后台初始化 - 阶段 3 完成 ✓")
            
            # 阶段 4: 创建 RagService 实例（依赖 chat_model + vector_store + reranker）
            await self._init_rag_service()
            logger.info("后台初始化 - 阶段 4: RagService 完成 ✓")
            
        except Exception as e:
            self.init_error = str(e)
            logger.error(f"后台初始化失败: {e}")

    async def _init_models(self):
        """初始化 AI 模型（Chat + Embedding + Vision + Classifier + Plan）"""
        try:
            from app.model_config.snapshots import load_snapshot
            from app.db.database import async_session_factory
            async with async_session_factory() as db:
                if await load_snapshot(db) is not None:
                    logger.info('模型从数据库发布按请求解析，跳过旧环境变量模型初始化')
                    self.classifier_model=None
                    self.plan_model=None
                    return
            from app.utils.factory import (
                create_chat_model, create_embed_model,
                create_classifier_model, create_plan_model, create_vision_model,
                create_autocomplete_model,
            )
            self.chat_model = create_chat_model(enable_thinking=False)  # 默认关闭思考
            try:
                # 深度思考实例（供前端开关使用），失败不影响核心功能
                self.chat_model_thinking = create_chat_model(enable_thinking=True)
                logger.info("深度思考模型初始化成功")
            except Exception as e:
                logger.warning(f"深度思考模型初始化失败: {e}")
                self.chat_model_thinking = None
            self.embed_model = create_embed_model()
            # 视觉模型（图片/视频理解），失败优雅降级：附件仍可上传/展示，但 AI 无法理解
            try:
                self.vision_model = create_vision_model()
                logger.info("视觉模型初始化成功")
            except Exception as e:
                logger.warning(f"视觉模型初始化失败，图片/视频理解不可用: {e}")
                self.vision_model = None
            # 分类器和 Plan 模型（轻量级，失败不影响核心功能）
            try:
                self.classifier_model = create_classifier_model()
                logger.info("分类器模型初始化成功")
            except Exception as e:
                logger.warning(f"分类器模型初始化失败，L2 分类不可用: {e}")
                self.classifier_model = None
            try:
                self.plan_model = create_plan_model()
                logger.info("Plan 模型初始化成功")
            except Exception as e:
                logger.warning(f"Plan 模型初始化失败，Plan-Execute 不可用: {e}")
                self.plan_model = None
            # AI 联机补全模型（轻量，独立实例；AUTOCOMPLETE_ENABLED=false 时返回 None）
            try:
                self.autocomplete_model = create_autocomplete_model()
                if self.autocomplete_model is None:
                    logger.info("AI 联机补全已禁用（AUTOCOMPLETE_ENABLED=false）")
                else:
                    logger.info("补全模型初始化成功")
            except Exception as e:
                logger.warning(f"补全模型初始化失败，AI 联机补全不可用: {e}")
                self.autocomplete_model = None
            logger.info("AI 模型初始化成功")
        except Exception as e:
            logger.warning(f"AI 模型初始化失败（部分功能不可用）: {e}")
    
    async def _init_services(self):
        """初始化 NoteService 和 ChromaDB"""
        try:
            from app.rag.vector_store import VectorStoreService
            from app.services.note_service import NoteService
            
            vector_store = VectorStoreService(embed_model=self.embed_model)
            self.note_service = NoteService(
                vector_store=vector_store,
                chat_model=self.chat_model,
                # ★ C5：补全模型必须在阶段 2 注入，漏掉则 ai_autocomplete 恒返回空
                #（静默失效且无日志）
                autocomplete_model=self.autocomplete_model,
            )
            logger.info("NoteService + ChromaDB 初始化成功")
        except Exception as e:
            logger.warning(f"NoteService 初始化失败: {e}")
    
    async def _init_reranker(self):
        """初始化重排序模型（带 30 秒超时保护）"""
        try:
            from app.utils.config import get_chroma_config
                
            config = get_chroma_config().get("reranker", {})
            if not config.get("enabled", True):
                self.reorder_service = None
                logger.info("本地配置已关闭 RAG 重排")
                return
            model_name = config.get("model_name", "BAAI/bge-reranker-v2-m3")
                
            def load_reranker():
                from sentence_transformers import CrossEncoder
                return CrossEncoder(model_name)

            self.reorder_service = await asyncio.wait_for(asyncio.to_thread(load_reranker), timeout=30)
            logger.info(f"重排序模型初始化成功: {model_name}")
        except asyncio.TimeoutError:
            logger.warning("重排序模型初始化超时（30s），跳过。知识库重排序功能不可用。")
        except Exception as e:
            logger.warning(f"重排序模型初始化失败: {e}")
    
    async def _init_rag_service(self):
        """创建 RagService 实例（复用，避免每次请求重建）"""
        try:
            from app.rag.vector_store import VectorStoreService
            from app.rag.rag_service import RagService
            from app.utils.config import get_rag_config

            vector_store = VectorStoreService()
            rag_config = get_rag_config()
            self.rag_service = RagService(
                vector_store=vector_store,
                chat_model=self.chat_model,
                rerank_model=self.reorder_service,
                enable_summarize=rag_config.get("enable_summarize", False),
                enable_published_expansion=rag_config.get("query_expansion_enabled") is True,
            )
            logger.info("RagService 实例创建成功")
        except Exception as e:
            logger.warning(f"RagService 创建失败: {e}")

    async def init_mcp(self):
        """
        初始化 MCP 工具（独立后台任务，与模型加载并行，互不阻塞）

        任一 MCP Server 失败仅降级跳过（实施文档 §7 风险 #5），
        不影响 Agent 主流程与其他初始化阶段。
        """
        try:
            from app.ai_service.mcp_manager import mcp_manager
            self.mcp_manager = mcp_manager
            await mcp_manager.start()
        except Exception as e:
            logger.error(f"MCP 工具初始化失败（联网搜索/网页抓取不可用）: {e}")

    async def close_mcp(self):
        """关闭 MCP 客户端（应用关闭阶段调用，幂等）"""
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.close()
            except Exception as e:
                logger.warning(f"MCP 客户端关闭异常: {e}")


# 全局后台初始化管理器
init_manager = BackgroundInitManager()


# ========== FastAPI 应用 ==========

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.rag.chroma_transport import validate_routes
    from app.rag.maintenance_lock import local_vector_owner

    validate_routes()
    with local_vector_owner():
        async with _application_lifespan(app):
            yield


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    """
    应用生命周期管理
    
    启动阶段：
    1. 初始化数据库表结构
    2. 连接 Redis
    3. 后台异步初始化模型和重型资源
    
    关闭阶段：
    1. 关闭 Redis 连接池
    2. 关闭数据库连接池
    """
    # ===== 启动阶段 =====
    logger.info("=" * 50)
    logger.info("RAG LearnLittle 启动中...")
    logger.info("=" * 50)

    # 0. 安全校验：JWT_SECRET 必须是强随机值（公开默认密钥直接拒绝启动）
    from app.utils.auth_utils import validate_jwt_secret
    validate_jwt_secret()

    # 1. 初始化数据库
    await init_db()

    # 2. 启动 model_trace 输出总线（成本账单落库 worker）+ 种子模型定价
    start_trace_bus()
    from app.services.usage_service import seed_model_pricing
    await seed_model_pricing()

    # 3. 连接 Redis
    redis = await init_redis()
    app.state.redis = redis
    from app.model_config.router import service as config_control
    from app.model_config.outbox import ConfigOutboxWorker, CredentialRevocationWatcher
    from app.db.database import async_session_factory
    config_outbox=ConfigOutboxWorker(config_control,async_session_factory,redis=redis)
    config_outbox.start()
    credential_watcher = CredentialRevocationWatcher(async_session_factory, redis=redis)
    credential_watcher.start()
    from app.model_config.lifecycle import configured_worker
    candidate_worker,candidate_engine=configured_worker(config_control,async_session_factory)
    if candidate_worker:candidate_worker.start()
    from app.user_memory.router import service as memory_control
    from app.user_memory.jobs import MemoryWorker
    memory_worker=MemoryWorker(memory_control,async_session_factory)
    memory_worker.start()

    # 4. 创建测试用户（仅非生产环境；生产环境需显式 ENABLE_TEST_USER=true 才创建）
    app_env = _clean_env("APP_ENV", "development", ["development", "production"])
    enable_test_user = os.getenv("ENABLE_TEST_USER", "false").lower() in ("true", "1", "yes")
    if app_env != "production" or enable_test_user:
        await _create_test_user()
    else:
        logger.info("生产环境（APP_ENV=production）：跳过测试用户创建，如需创建请设置 ENABLE_TEST_USER=true")

    # 5. 后台异步初始化重型资源（完成后标记就绪，供 /ready 探针使用）
    async def _run_init_and_mark_ready():
        try:
            await init_manager.run()
        except Exception as e:
            # run() 内部已捕获异常，此处仅兜底，避免任务异常导致 /ready 永不就绪
            logger.error(f"后台初始化任务异常: {e}")
        finally:
            app.state.init_complete = True
            app.state.init_error = init_manager.init_error

    asyncio.create_task(_run_init_and_mark_ready())

    from app.ai_service.detached_worker import DetachedChatWorker
    from app.routers.chat import stream_queued_chat
    detached_worker = DetachedChatWorker(async_session_factory, stream_queued_chat)
    detached_worker.start()

    from app.rag.ingest_worker import KnowledgeIngestWorker
    from app.rag.index_build_worker import IndexBuildWorker
    from app.rag.note_index_worker import NoteIndexWorker
    knowledge_worker = KnowledgeIngestWorker(async_session_factory)
    note_index_worker = NoteIndexWorker(async_session_factory)
    index_build_worker = IndexBuildWorker(config_control, async_session_factory)
    knowledge_worker.start()
    note_index_worker.start()
    index_build_worker.start()

    # 5.1 后台初始化 MCP 工具（独立任务，与模型加载并行；失败仅降级，不影响主流程）
    asyncio.create_task(init_manager.init_mcp())

    # 6. 启动定时任务（reload 模式跳过，避免 uvicorn fork 子进程导致重复执行）
    reload = os.getenv("UVICORN_RELOAD", "false").lower() in ("true", "1", "yes")
    if not reload:
        from app.core.scheduler import init_scheduler
        init_scheduler()
        logger.info("定时任务调度器已启动（非 reload 模式）")
    else:
        logger.info("检测到 reload 模式，跳过 scheduler 启动（避免重复执行）")

    logger.info("应用启动完成，后台初始化进行中...")

    yield

    # ===== 关闭阶段 =====
    logger.info("应用关闭中...")
    from app.ai_service.detached_chat import stop_detached_chats
    await detached_worker.stop()
    await knowledge_worker.stop()
    await note_index_worker.stop()
    await index_build_worker.stop()
    await stop_detached_chats()
    # 关闭 MCP 客户端（0.3.x 无持久连接，close 为幂等清理）
    await config_outbox.stop()
    await credential_watcher.stop()
    if candidate_worker:await candidate_worker.stop()
    if candidate_engine:await candidate_engine.dispose()
    await memory_worker.stop()
    await init_manager.close_mcp()
    # 关闭定时任务调度器（wait=False 避免阻塞关闭流程）
    if not reload:
        from app.core.scheduler import shutdown_scheduler
        shutdown_scheduler()
    # 关闭共享 httpx 连接池
    from app.utils.factory import close_shared_http_client
    await close_shared_http_client()
    # 排空 model_trace DB 队列（worker 停止）
    await stop_trace_bus()
    await close_redis()
    await close_db()
    logger.info("应用已关闭")


async def _create_test_user():
    """
    创建测试用户 admin/admin1234（仅开发/测试环境，由 lifespan 按 APP_ENV 门控）

    如果用户已存在则跳过。
    注意：生产环境默认不调用本函数；即使显式启用也不得使用该弱口令，应通过管理脚本初始化。
    """
    from app.db.database import async_session_factory
    from app.models.user import User
    from app.utils.auth_utils import hash_password
    from sqlalchemy import select
    
    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.username == "admin"))
        if result.scalar_one_or_none():
            logger.info("测试用户 admin 已存在，跳过创建")
            return
        
        user = User(
            username="admin",
            password=hash_password("admin1234"),
            email="admin@raglearn.local",
        )
        session.add(user)
        await session.commit()
        logger.info("测试用户创建成功: admin（仅开发/测试环境）")


# 创建 FastAPI 应用
app = FastAPI(
    title="RAG LearnLittle",
    description="AI 驱动的个人知识管理工具 — 智能笔记助手 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ========== 中间件 ==========

# CORS 中间件
# 浏览器规范禁止 allow_origins=["*"] 与 allow_credentials=True 组合：
# 带凭据时中间件会回显任意请求 Origin，等于允许任意站点跨域带凭据调用。
# 因此出现 "*" 时强制关闭 credentials（并告警），生产建议显式配置域名白名单。
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
allow_credentials = True
if "*" in cors_origins:
    cors_origins = ["*"]
    allow_credentials = False
    logger.warning(
        "CORS_ORIGINS 使用通配符 '*'，已关闭 allow_credentials（凭据请求将不被允许）。"
        "生产环境建议配置具体前端域名白名单，如: CORS_ORIGINS=https://app.example.com"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag", "X-Request-ID", "X-Workflow-Id", "X-Knowledge-Job-Id"],
)


# 请求处理时间中间件
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """
    记录每个请求的处理时间，添加到响应头 X-Process-Time 中。
    同时记录请求日志（method + path + status + duration_ms）。
    """
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = f"{process_time:.4f}"
    
    # 记录请求日志
    logger.info(
        f"{request.method} {request.url.path} - "
        f"status={response.status_code} "
        f"duration={process_time * 1000:.1f}ms"
    )
    
    return response


# Prometheus 指标采集中间件（审查 P1-2；注意需在 CORS 中间件之后注册）
app.add_middleware(MetricsMiddleware)
from app.model_config.snapshots import SnapshotMiddleware
app.add_middleware(SnapshotMiddleware)

# 注册全局异常处理器
register_exception_handlers(app)


# ========== 路由注册 ==========

from app.routers import (
    health,
    user,
    chat,
    note_router,
    category_router,
    knowledge_router,
    review_router,
    note_template_router,
    usage,
    ppt_router,
    ppt_template_router,
    tts_router,
)

# 健康检查（不需要认证）
app.include_router(health.router, tags=["Health"])

# 业务路由（统一 /api/v1 前缀）
API_PREFIX = "/api/v1"

app.include_router(user.router, prefix=API_PREFIX, tags=["User & Auth"])
app.include_router(chat.router, prefix=API_PREFIX, tags=["Chat"])
app.include_router(note_router.router, prefix=API_PREFIX, tags=["Note"])
app.include_router(category_router.router, prefix=API_PREFIX, tags=["Category"])
app.include_router(knowledge_router.router, prefix=API_PREFIX, tags=["Knowledge"])
app.include_router(review_router.router, prefix=API_PREFIX, tags=["Review"])
app.include_router(note_template_router.router, prefix=API_PREFIX, tags=["Note Template"])
app.include_router(usage.router, prefix=API_PREFIX, tags=["Usage"])
app.include_router(ppt_router.router, prefix=API_PREFIX, tags=["PPT"])
app.include_router(ppt_template_router.router, prefix=API_PREFIX, tags=["PPT"])
app.include_router(tts_router.router, prefix=API_PREFIX, tags=["TTS"])
from app.model_config.router import router as model_config_router, auth_router as model_config_auth_router
app.include_router(model_config_router, prefix=API_PREFIX)
app.include_router(model_config_auth_router, prefix=API_PREFIX)
from app.user_memory.router import router as memory_router,MemoryPrivacyMiddleware
app.include_router(memory_router,prefix=API_PREFIX)
app.add_middleware(MemoryPrivacyMiddleware)

# 静态文件服务（头像访问）
# 确保头像存储目录存在
avatar_dir = os.path.join("data", "avatars")
os.makedirs(avatar_dir, exist_ok=True)
app.mount("/static/avatars", StaticFiles(directory=avatar_dir), name="avatars")


# ========== 启动入口 ==========

def _clean_env(key: str, default: str, valid_values: list = None) -> str:
    """
    安全读取环境变量，自动去除内联注释（以 # 开头的内容）

    python-dotenv 不支持行内 # 注释，会把整行当作值。
    此函数以 # 分割取第一部分，并去除首尾空白。

    Args:
        key: 环境变量名
        default: 默认值
        valid_values: 可选的合法值列表，不在此列表内的值会退回 default
    """
    raw = os.getenv(key, default)
    if not raw:
        return default
    # 去除 # 及其后的内容（.env 文件不支持内联注释）
    value = raw.split("#")[0].strip().lower()
    if valid_values and value not in valid_values:
        return default
    return value


if __name__ == "__main__":
    import logging
    import uvicorn
    from logging.handlers import TimedRotatingFileHandler
    from pathlib import Path

    # 热重载（审查 P1-2：生产默认关闭；开发时显式设置 UVICORN_RELOAD=true 开启）
    # 说明：reload=True 会 fork 子进程，导致 VSCode 断点无法命中，
    #       且生产环境以 reload 模式运行会带来双进程守护与热重载风险
    reload = os.getenv("UVICORN_RELOAD", "false").lower() in ("true", "1", "yes")

    # 安全解析 LOG_LEVEL（防止内联注释被当作值，如 "DEBUG  # 注释"）
    _valid_log_levels = ["critical", "error", "warning", "info", "debug", "trace"]
    log_level = _clean_env("LOG_LEVEL", "info", _valid_log_levels)

    # ---- 将 uvicorn 日志输出到 logs/ 目录 ----
    # 避免根目录产生 backend_err.log / backend_out.log 等散落文件
    _log_dir = Path("logs")
    _log_dir.mkdir(exist_ok=True)

    class _SimpleFormatter(logging.Formatter):
        """uvicorn 日志格式化器（简洁格式，与 uvicorn 自带控制台输出风格一致）"""
        def __init__(self):
            super().__init__(fmt="%(asctime)s %(levelname)s:  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # uvicorn 日志配置：控制台 + 文件双输出（文件按日轮转，保留 30 天）
    # 注意：不能先 logging.getLogger("uvicorn").addHandler(...) 再 uvicorn.run() ——
    # uvicorn.run 内部的 dictConfig 会重建 uvicorn/uvicorn.error/uvicorn.access
    # 三个 logger 并清掉手动添加的 handler（实测 logs/uvicorn.log、access.log 一直为空）。
    # 正确做法：把文件 handler 写进 log_config 字典传给 uvicorn.run。
    _uvicorn_log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"()": _SimpleFormatter},
            "access": {"()": _SimpleFormatter},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "console_access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "stream": "ext://sys.stderr",
            },
            "file_uvicorn": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "formatter": "default",
                "filename": str(_log_dir / "uvicorn.log"),
                "when": "midnight",
                "interval": 1,
                "backupCount": 30,
                "encoding": "utf-8",
            },
            "file_access": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "formatter": "access",
                "filename": str(_log_dir / "access.log"),
                "when": "midnight",
                "interval": 1,
                "backupCount": 30,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["console", "file_uvicorn"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["console_access", "file_access"], "level": "INFO", "propagate": False},
        },
    }

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(_clean_env("PORT", "8000")),
        reload=reload,
        log_level=log_level,
        log_config=_uvicorn_log_config,
    )
