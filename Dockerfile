# ========== RAG LearnLittle 后端镜像（多阶段构建） ==========
# 构建: docker build -t raglearn-backend .
#
# 国内服务器构建加速（可选；也可写进 .env 的 PIP_INDEX_URL，由 compose 自动透传）:
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t raglearn-backend .
#
# 可选预下载重排序模型（约 1GB，需构建期能访问 HuggingFace）:
#   docker build --build-arg PRELOAD_MODELS=true -t raglearn-backend .
#
# 模型缓存路径（重要，v1.1 修正）:
#   容器以非 root（appuser, uid 1000）运行，/root 不可遍历，因此缓存统一放在
#   HF_HOME=/opt/hf-cache。挂载本机缓存请挂到该路径:
#     -v $HOME/.cache/huggingface:/opt/hf-cache
#   注意: 2G 内存服务器不要挂载/预载（重排序模型 fp32 需 2GB+ 内存），
#         详见 docs/宝塔Linux服务器Docker部署方案.md §2.1

# ---- 阶段 1: 安装 Python 依赖 ----
FROM python:3.12-slim AS builder

WORKDIR /app

# pip 源：国内服务器构建建议改用镜像源，否则 requirements.lock 安装极慢/易失败
ARG PIP_INDEX_URL=https://pypi.org/simple

# 系统库: libmagic（langchain document_loaders 延迟导入可选使用，python-magic-bin 已移除）、
#        ffmpeg（视频抽帧）、构建工具（部分 wheel 需编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libmagic1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 使用锁定版本安装（requirements.lock 由 pip freeze 生成，保证可复现）
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.lock

# 可选: 预下载重排序模型到镜像（HF_HUB_OFFLINE 在运行时由入口环境变量强制开启，
# 模型必须在本地缓存中存在；此处构建期联网下载一次）
# 先建目录：保证 PRELOAD_MODELS=false 时下一阶段的 COPY 仍有源路径可拷贝，避免构建失败
ARG PRELOAD_MODELS=false
RUN mkdir -p /root/.cache/huggingface \
    && if [ "$PRELOAD_MODELS" = "true" ]; then \
        python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-v2-m3')"; \
    fi

# ---- 阶段 2: 运行时镜像 ----
FROM python:3.12-slim AS runtime

WORKDIR /app

# 运行时系统库（不装构建工具，控制镜像体积）
# gosu: 入口脚本以 root 修正挂载目录属主后，降权为 appuser 运行应用（见 scripts/docker-entrypoint.sh）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/* \
    # MCP server 运行时: uvx（mcp-server-fetch）+ npx（tavily-mcp）
    && pip install --no-cache-dir uv \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 阶段复制已安装的依赖
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
# 模型缓存落到 /opt/hf-cache：与 HF_HOME 一致，且非 root 用户可读
COPY --from=builder /root/.cache/huggingface /opt/hf-cache

# 应用代码（.dockerignore 已排除 .venv/data/logs/front/node_modules 等）
COPY . .

# 非 root 用户（uid 1000，与 compose bind-mount 目录预期属主一致）
# 入口脚本先做 CRLF→LF 归一化：Windows 检出（core.autocrlf=true）可能带入 CRLF，
# 会导致 `#!/bin/sh\r` 解释器找不到、容器启动即失败（.gitattributes 已兜底，此处双保险）
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /app/logs /app/media /opt/hf-cache \
    && chown -R appuser:appuser /app /opt/hf-cache \
    && sed -i 's/\r$//' /app/scripts/docker-entrypoint.sh \
    && chmod +x /app/scripts/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PORT=8000

EXPOSE 8000

# 健康检查（/health 为存活探针；/ready 为就绪探针，由编排层使用）
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# 入口脚本：容器以 root 启动 → 修正 data/logs/media 属主 → gosu 降权为 appuser 执行 CMD
# （应用进程本身仍以非 root 运行，只是多了一层属主修正）
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]

# 生产启动方式（reload 必须关闭；workers 单进程部署，多 worker 需先改造限流/scheduler）
# 启动前先执行数据库迁移（init_db 内部也会执行，此处提前执行保证迁移在服务暴露前完成）
# 末尾用 exec 让 uvicorn 直接替换 sh 成为 PID 1，保证 docker stop 的 SIGTERM
# 能送达 uvicorn 触发优雅关闭（否则 sh 不转发信号，只能等 10s 后被 SIGKILL）
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log"]
