#!/bin/sh
# ========== 后端容器入口脚本 ==========
#
# 职责：容器以 root 启动 → 修正 bind-mount 目录属主 → 降权为 appuser 运行 CMD。
#
# 为什么必须做这一步（否则服务器部署会出现两个硬故障）：
#
#   1) logs/ 不可写 → 容器反复重启（P0）
#      app/core/logger_handler.py 在 **模块导入期** 就创建 TimedRotatingFileHandler
#      往 <项目根>/logs/app.log 写入（无 try/except）。而 data/、logs/、huggingface_cache/
#      都被 .gitignore 忽略，git clone 后并不存在，是被 Docker 以 root:root 属主
#      自动创建并 bind-mount 进容器的；容器内进程是 uid 1000
#      → PermissionError → 导入 main 即崩溃 → 无限重启。
#
#   2) data/ 不可写 → ChromaDB 持久化与文件上传失败
#      data/chroma 向量库、data/uploads、data/avatars 等均需写权限。
#
# 修正策略：
#   - data / logs / media：递归 chown（这些目录被 .gitignore 忽略、clone 后不存在，
#     由 Docker 以 root:root 属主创建，容器内 appuser 无法写入）
#   - 模型缓存 /opt/hf-cache：同样递归 chown——huggingface_hub 会在缓存目录内创建
#     .locks 等辅助目录，只读权限下可能加载失败。目录为空时开销可忽略；
#     若挂载了 1GB+ 缓存，首次启动会多花几秒（仅影响启用重排序模型的 4G+ 服务器）。
#
# 该脚本幂等，重复执行无副作用；修正失败只告警不中断（保留排查线索）。

set -e

APP_USER="appuser"

# ---------- 1. 需要写权限的目录：递归修正属主 ----------
for d in /app/data /app/logs /app/media /opt/hf-cache; do
    mkdir -p "$d" 2>/dev/null || true
    if ! chown -R "$APP_USER:$APP_USER" "$d" 2>/dev/null; then
        echo "[entrypoint] 警告: 无法修正 $d 属主（应用可能报权限错误）" >&2
    fi
done

# ---------- 2. 降权执行 CMD ----------
# gosu 使用 exec 替换进程，保证 SIGTERM/SIGINT 能正确传递给 uvicorn（优雅关闭）
if ! command -v gosu >/dev/null 2>&1; then
    echo "[entrypoint] 警告: 未找到 gosu，退回以 root 运行（建议检查镜像构建）" >&2
    exec "$@"
fi

exec gosu "$APP_USER" "$@"
