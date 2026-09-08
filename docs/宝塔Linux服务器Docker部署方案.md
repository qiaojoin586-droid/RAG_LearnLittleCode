# 宝塔 Linux 服务器 Docker 部署方案

> 版本：v1.1（2026-09-08）
> 适用环境：宝塔 Linux 面板（uxue 服务器，2核 2G 内存），几十人规模使用，暂无域名/未备案
> 部署方式：Docker Compose（项目已完整容器化：mysql + redis + backend + frontend/nginx）

### 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-08-22 | 首版 |
| v1.1 | 2026-09-08 | 配合 Docker 文件更新：① 前端 nginx 模式改为 `NGINX_CONF` 构建参数切换，**不再需要手工替换 nginx.conf**（§7）；② 新增国内构建加速参数 `PIP_INDEX_URL` / `NPM_REGISTRY`（§6.1）；③ 模型缓存路径由 `/root/.cache/huggingface` 修正为 `/opt/hf-cache`（容器非 root，原路径不可读）（§2.1）；④ 新增容器属主自动修正说明，**不再需要手工 chown**（§6.2）；⑤ 后端 8000 端口改为仅绑定回环、容器日志启用轮转（§8）；⑥ 新增出网代理章节（OpenAI / 外网模型）（§14） |

---

## 目录

1. [部署方式选型：为什么用 Docker](#1-部署方式选型为什么用-docker)
2. [2核2G 服务器的三个现实约束](#2-2核2g-服务器的三个现实约束)
3. [无域名阶段的处理](#3-无域名阶段的处理)
4. [服务器环境准备](#4-服务器环境准备)
5. [获取代码](#5-获取代码)
6. [配置 .env](#6-配置-env)
7. [切换前端 nginx 模式（无需手改文件）](#7-切换前端-nginx-模式无需手改文件)
8. [构建与启动](#8-构建与启动)
9. [验证部署](#9-验证部署)
10. [端口放行](#10-端口放行)
11. [上线后运维（备份/监控/更新）](#11-上线后运维备份监控更新)
12. [域名备案后切换 HTTPS](#12-域名备案后切换-https)
13. [常见问题排查](#13-常见问题排查)
14. [出网代理（OpenAI 等外网模型）](#14-出网代理openai-等外网模型)


---

## 1. 部署方式选型：为什么用 Docker

**结论：推荐 Docker Compose 部署，不建议在服务器上手工安装 Python/Node/MySQL/Redis 环境。**

理由：

| 对比项 | Docker Compose | 手工部署 |
|--------|---------------|---------|
| 环境一致性 | 镜像内锁定 Python 3.12 + requirements.lock + Node 22，与本地完全一致 | 需在服务器逐个装 Python、Node、MySQL 8、Redis 7，版本漂移风险大 |
| 一条命令起全套 | `docker compose up -d` 拉起 4 个服务 | 手动配置 systemd、开机自启、依赖顺序 |
| SSE 流式支持 | `front/nginx.conf` 已配置 `proxy_buffering off`（/api 反代），对话流式输出开箱即用 | 需自己配宝塔 nginx 反代，漏配 `proxy_buffering off` 会导致流式输出被缓冲卡住 |
| 数据持久化 | mysql_data / redis_data 卷 + ./data、./logs 目录挂载，升级重建容器数据不丢 | 与系统环境耦合，迁移困难 |
| 回滚 | 镜像版本可回退 | 困难 |

项目现状（已核查，v1.1 更新）：

- `docker-compose.yml`：四服务编排，含健康检查、依赖顺序（backend 等 mysql/redis healthy）、`restart: unless-stopped`；容器日志已启用轮转（单文件 10MB × 3）；后端 8000 端口仅绑定 `127.0.0.1`
- `Dockerfile`：多阶段构建，启动前自动执行 `alembic upgrade head` 数据库迁移，`/health` 健康探针。应用进程以非 root（`appuser`, uid 1000）运行；容器入口脚本 `scripts/docker-entrypoint.sh` 先修正挂载目录属主再降权执行（因此**无需在宿主机手工 chown**）
- `front/Dockerfile`：node:22 构建 + nginx:1.27 托管，SPA 路由回退已配；nginx 配置由构建参数 `NGINX_CONF` 在 HTTP / HTTPS 两套配置间切换
- 注意：仓库内**没有** `docker-compose.prod.yml`，也不需要它——生产覆盖通过 `.env` 中 `APP_ENV=production`、`LOG_LEVEL=INFO`、`LOG_FORMAT=json` 实现即可

**宝塔面板的角色**：只用它的端口放行（安全）、资源监控、计划任务（备份）功能。**不要**用宝塔的"网站/nginx"功能去反代本项目——前端容器自带 nginx 且占用 80 端口，宝塔 nginx 会与之冲突。

---

## 2. 2核2G 服务器的三个现实约束

### 2.1 重排序模型装不下 —— 会自动降级，无需处理

`main.py` 启动时（阶段 3）会加载 CrossEncoder 重排序模型 `BAAI/bge-reranker-v2-m3`（配置于 `config/chroma.yaml`），该模型 fp32 加载需要 **2GB+ 内存**，本服务器装不下。

**代码已有优雅降级**：运行镜像强制 `HF_HUB_OFFLINE=1`，模型不在本地缓存时 `_init_reranker` 捕获异常、仅记录 warning 后跳过（`main.py:179-198`）。RAG 检索流程照常工作，只是没有重排序打分，几十人使用场景质量损失可接受。

**因此部署时注意两条红线**：

1. 构建后端镜像**不要**设置 `PRELOAD_MODELS=true`（会把 ~1GB 模型打进镜像并在运行时吃满内存）
2. **不要**向 `huggingface_cache` 目录（挂载到容器 `/opt/hf-cache`）放入任何模型缓存，部署时留空目录即可

> **v1.1 修正**：模型缓存挂载点由 `/root/.cache/huggingface` 改为 `/opt/hf-cache`。原路径位于容器内 root 家目录（`/root` 权限 700），而应用以 uid 1000 运行，**既无法遍历目录也无法读取文件**，导致挂载或预载的模型永远加载不到——表现为启动日志里始终出现"重排序模型初始化失败"。新路径与镜像内的 `HF_HOME` 一致，且属主为 appuser。

> 后续如果检索质量确实不满意，两条升级路径：① 服务器加内存到 4G 后挂载模型缓存启用重排序；② 换用轻量重排序模型（需修改 `config/chroma.yaml` 的 `reranker.model_name`）。

### 2.2 必须加 swap

内存预算：

| 组件 | 预估内存 |
|------|---------|
| 系统 + 宝塔面板 | ~300MB |
| MySQL 8.0 容器 | ~400MB |
| Redis 容器 | ~50MB |
| 后端容器（FastAPI + LangChain，不含重排序模型） | ~400-500MB |
| 前端 nginx 容器 | ~20MB |
| **合计** | **~1.2-1.3GB**（剩余余量 < 800MB） |

余量过紧，**必须在部署前加 4G swap**（见 §4 第 3 步），否则构建期或流量高峰期容易触发 OOM Killer。

### 2.3 服务器上构建前端镜像有 OOM 风险

前端 `npm ci + tsc 类型检查 + vite build` 对 3000+ 源文件的工程，构建峰值内存可能超过 1.5GB。两种方案：

- **方案 A（推荐）：本地构建、传输镜像**
  ```bash
  # 本地（你的开发机）
  docker compose build backend frontend
  docker save raglearn-backend raglearn-front | gzip > raglearn-images.tar.gz
  scp raglearn-images.tar.gz root@服务器IP:/opt/

  # 服务器
  docker load < /opt/raglearn-images.tar.gz
  docker compose up -d
  ```
  优点：服务器零构建压力；缺点：镜像传输 ~2-3GB（走 scp 或对象存储中转）。

- **方案 B：服务器直接构建**
  ```bash
  docker compose up -d --build
  ```
  在已加 4G swap 的前提下通常能过，但耗时长（2 核编译 + npm install 约 10-20 分钟），且有一定失败率。失败就回退方案 A。

---

## 3. 无域名阶段的处理

### 3.1 现状问题

`front/nginx.conf` 默认**强制 HTTPS**：

- 80 端口 `return 301 https://...` 全部跳转
- 443 端口 `ssl_certificate` 指向 `/etc/nginx/certs/` 下的证书，**不挂载证书 nginx 启动直接失败**

而 Let's Encrypt 不为裸 IP 签发证书，所以**没有域名时必须改用纯 HTTP 配置**。

项目已随仓库提供 `front/nginx.http.conf`（纯 HTTP 版，SSE 相关配置完整保留），通过 `.env` 的 `NGINX_CONF` 构建参数切换即可，**无需手工改文件**（见 §7）。

### 3.2 合规说明

- ICP 备案绑定的是**域名**，不是 IP。国内服务器裸 IP 访问一般不要求备案，但部分云服务商对未备案 Web 服务有端口管控，属正常现象
- 当前阶段用户通过 `http://服务器IP` 访问，明文 HTTP 下 JWT token 存在被同网络嗅探的理论风险，几十人内部使用可接受，**对外正式提供服务前务必按 §12 切换 HTTPS**
- 域名购买 → 备案（通常 1-3 周）→ DNS 解析到服务器 IP → 按 §12 恢复 HTTPS 配置

---

## 4. 服务器环境准备

SSH 登录服务器后依次执行：

```bash
# ===== 1. 安装 Docker（国内走阿里云镜像脚本）=====
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
systemctl enable --now docker
docker --version          # 确认安装成功

# ===== 2. 配置镜像加速（拉取 mysql/redis/nginx 镜像用）=====
mkdir -p /etc/docker
echo '{"registry-mirrors":["https://docker.m.daocloud.io"]}' > /etc/docker/daemon.json
systemctl restart docker

# ===== 3. 加 4G swap（2G 内存必做）=====
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h                   # 确认 Swap 一行显示 4.0Gi

# ===== 4. 安装 git =====
apt update && apt install -y git     # CentOS 系: yum install -y git

# ===== 5.（可选）安装宝塔面板 =====
# 若已安装则跳过；若新装：
curl -sSO https://raw.githubusercontent.com/8838/btpanel-v7.7.0/main/install/install_panel.sh && bash install_panel.sh
# 注意：宝塔自带的 nginx 若已运行会占用 80 端口，与前端容器冲突
nginx -v 2>/dev/null && systemctl stop nginx && systemctl disable nginx
```

**宝塔使用边界**：

- ✅ 用：「安全」页放行端口、「监控」页看资源、计划任务做备份
- ❌ 不用：网站管理、nginx 反代、MySQL/Python 项目管理（全部由容器自包含）

---

## 5. 获取代码

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/<你的用户名>/RagLearnCode.git
cd RagLearnCode
```

GitHub 直连慢的处理方式（任选其一）：

1. **代理加速**：`git clone https://ghproxy.net/https://github.com/<你的用户名>/RagLearnCode.git`
2. **SSH + 443 端口**：在 `~/.ssh/config` 配置 `Host github.com / Port 443 / HostName ssh.github.com`，改用 SSH 地址克隆
3. **Gitee 镜像**：将仓库同步到 Gitee 后从国内克隆（更新需手动同步）

> 后续代码更新：`git pull` 后按 §11.3 的更新流程重建容器。

---

## 6. 配置 .env

```bash
cp .env.example .env
vim .env    # 或宝塔文件管理器直接编辑
```

**必改项**：

| 变量 | 值 | 说明 |
|------|-----|------|
| `MODEL_PROVIDER` | `dashscope` | **必须用云端 API**。2G 内存绝不能设为 ollama（本地模型会直接 OOM） |
| `DASHSCOPE_API_KEY` | 你的百炼 API Key | 阿里云百炼控制台获取 |
| `MYSQL_PASSWORD` | 强密码 | compose 用它创建 MySQL root，勿用默认 123456 |
| `JWT_SECRET` | 64 位随机串 | 生成命令：`python -c "import secrets; print(secrets.token_hex(32))"`。留空或默认值会拒绝启动 |
| `APP_ENV` | `production` | 不自动创建 admin/admin1234 测试账号 |
| `CORS_ORIGINS` | `http://服务器IP` | 前后端同源走 nginx 反代，此项影响面小，但生产不建议留 `*` |
| `LOG_LEVEL` | `INFO` | 生产日志级别 |
| `LOG_FORMAT` | `json` | 结构化日志，便于排查 |
| `NGINX_CONF` | `nginx.http.conf` | 无域名阶段必须设置（纯 HTTP）。域名备案完成后改回 `nginx.conf`（默认值） |
| `PIP_INDEX_URL` | `https://pypi.tuna.tsinghua.edu.cn/simple` | 国内服务器构建加速，见 §6.1 |
| `NPM_REGISTRY` | `https://registry.npmmirror.com` | 国内服务器构建加速，见 §6.1 |

**按需配置**（不配则对应功能返回"未配置"提示，不影响主流程）：

- `SMTP_*`：注册/登录邮件验证码（QQ 邮箱授权码，非登录密码）
- `TAVILY_API_KEY`：Agent 联网搜索工具
- `DEEPL_API_KEY` / `WOLFRAM_APP_ID`：翻译/计算工具
- `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`：出网代理，仅当需要访问 OpenAI 等外网模型时启用，见 §14

**保持默认即可**：`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`UVICORN_RELOAD=false`、`PPT_ENGINE=python_pptx`、`PRELOAD_MODELS=false`。

### 6.1 国内构建加速（强烈建议）

服务器直接构建时，后端 `pip install -r requirements.lock` 与前端 `npm ci` 默认走境外源，在 2 核小机器上极易超时或失败（前端还叠加 OOM 风险，见 §2.3）。

`.env` 中补上镜像源即可，compose 会把它们作为构建参数透传（**无需改 Dockerfile**）：

```ini
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
NPM_REGISTRY=https://registry.npmmirror.com
```

其他可选镜像源：`https://mirrors.aliyun.com/pypi/simple/`、`https://mirrors.cloud.tencent.com/pypi/simple/`；npm 也可用 `https://registry.npm.taobao.org`（已迁移至 npmmirror）。

### 6.2 数据目录权限：无需手工 chown

`.gitignore` 已忽略 `data/`、`logs/`、`huggingface_cache/`，因此服务器 `git clone` 后这些目录**并不存在**，由 Docker 以 `root:root` 属主自动创建；而容器内应用以 uid 1000（appuser）运行，必然写不进去。应用在**导入期**就会创建日志文件 Handler（`app/core/logger_handler.py:176-189`，无 try/except），一旦 `logs/` 不可写会直接导致容器反复重启。

为此镜像内置入口脚本 `scripts/docker-entrypoint.sh`：**容器以 root 启动 → 自动修正 `data` / `logs` / `media` / `/opt/hf-cache` 属主 → 降权为 appuser 执行业务进程**。该步骤幂等、每次启动执行，因此宿主机上不需要任何手工 `chown`，`git pull` 后重建容器也不会再触发权限问题。

> 应用进程本身仍以非 root 运行，入口脚本仅在启动瞬间使用 root 权限做属主修正。若挂载了 1GB 以上的模型缓存，首次启动会多花几秒用于递归 chown（仅启用重排序模型的 4G+ 服务器会遇到）。



---

## 7. 切换前端 nginx 模式（无需手改文件）

前端镜像在**构建期**根据 `NGINX_CONF` 参数选择 nginx 配置文件（`front/Dockerfile` 的 `ARG NGINX_CONF`，由 `docker-compose.yml` 从 `.env` 透传），因此不需要再手工替换 `front/nginx.conf`。

| 场景 | `.env` 设置 | 生效的配置文件 | 说明 |
|------|------------|---------------|------|
| 无域名 / 无证书（当前阶段） | `NGINX_CONF=nginx.http.conf` | `front/nginx.http.conf` | 纯 HTTP，80 端口直接提供服务 |
| 域名 + 正式证书 | `NGINX_CONF=nginx.conf`，或删掉该行（默认） | `front/nginx.conf` | 80 跳 443 + TLS + HSTS |

两套配置的差异：

- `nginx.http.conf`：删除 80 → 443 的 301 跳转、删除整个 443 ssl server 块（无证书时 nginx 会直接启动失败），HSTS 响应头一并去掉
- 两套配置**都完整保留** `/api/` 下的 `proxy_buffering off`、`proxy_cache off`、300s 读写超时 —— 这是对话 SSE 流式输出正常工作的前提，任何情况下都不能删

修改 `.env` 后重建前端容器即可生效：

```bash
docker compose up -d --build frontend
```

> 临时 HTTP 阶段，443 端口映射与 `./certs` 卷都可以留着（无进程监听、目录为空，不影响运行）。若服务器上 443 已被宝塔或其他服务占用、导致 compose 无法启动，把 `docker-compose.yml` 中 frontend 的 `"443:443"` 一行注释掉即可。


---

## 8. 构建与启动

### 方案 A：本地构建镜像传输（推荐）

```bash
# ---------- 本地开发机 ----------
cd <项目根目录>
docker compose build backend frontend
docker images | grep raglearn              # 确认 raglearn-backend / raglearn-front 镜像生成
docker save raglearn-backend raglearn-front | gzip > raglearn-images.tar.gz
scp raglearn-images.tar.gz root@服务器IP:/opt/

# ---------- 服务器 ----------
cd /opt/RagLearnCode
docker load < /opt/raglearn-images.tar.gz
docker compose up -d                       # mysql/redis 镜像自动从加速源拉取
```

### 方案 B：服务器直接构建

```bash
cd /opt/RagLearnCode
docker compose up -d --build
```

构建耗时预计 10-20 分钟（2 核限制）。若前端构建阶段被 OOM Kill（`docker compose logs` 或 `dmesg | grep -i oom` 可见），回退方案 A。

### 构建参数来源（两方案不同，注意）

`docker compose build` 会把 `.env` 中的构建参数透传给镜像，因此**在哪里构建，就要在哪里把参数配对**：

| 参数 | 方案 A（本地构建） | 方案 B（服务器构建） |
|------|------------------|--------------------|
| `NGINX_CONF` | **必须在构建机的 `.env` 里设为 `nginx.http.conf`**（前端 nginx 配置在构建期烧进镜像，服务器上的 `.env` 改它无效，需重新构建前端镜像） | 在服务器 `.env` 设置即可 |
| `PIP_INDEX_URL` / `NPM_REGISTRY` | 通常不需要（本地网络好） | **建议设置**，见 §6.1 |
| `PRELOAD_MODELS` | 保持 `false`（2G 服务器红线，见 §2.1） | 保持 `false` |

> 踩坑提示：若用方案 A 构建前端却忘了在构建机设 `NGINX_CONF=nginx.http.conf`，镜像里会是强制 HTTPS 的配置，服务器上表现为 frontend 容器反复重启（缺少证书）。此时把构建机 `.env` 补上该变量后重新构建前端即可。

### 首次启动行为说明

- 容器入口脚本 `scripts/docker-entrypoint.sh` 先以 root 修正 `data` / `logs` / `media` / `/opt/hf-cache` 属主，再降权为 appuser（uid 1000）执行业务进程；**幂等、每次启动执行**，宿主机无需手工 chown（见 §6.2）
- backend 容器启动命令为 `alembic upgrade head && uvicorn ...`，会先执行数据库迁移建表，**健康检查有 120s 宽限期**（`start-period=120s`），期间 `docker compose ps` 显示 unhealthy 属正常
- 首次启动 mysql 容器初始化约需 30 秒，backend 会等待其 healthy 后才开始
- 后端 8000 端口只绑定宿主机 `127.0.0.1`，Windows/Mac 上用浏览器访问 `http://127.0.0.1:8000` 仍可直连调试（局域网其他机器访问不到）


---

## 9. 验证部署

```bash
# 1. 容器状态：四个容器均应 running/healthy
docker compose ps

# 2. 后端启动日志（观察关键初始化阶段）
docker compose logs backend | grep -E "初始化|启动|ERROR|WARNING"

# 3. 容器内健康探针
docker exec raglearn-backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status)"

# 4. 确认业务进程以非 root 运行（应为 uid=1000(appuser)）
docker exec raglearn-backend id

# 5. 确认日志目录可写（应能成功创建并删除探针文件）
docker exec raglearn-backend sh -c "touch /app/logs/.probe && rm /app/logs/.probe && echo logs OK"

# 6. 确认容器日志轮转已生效（应输出 10m/3）
docker inspect raglearn-backend --format '{{json .HostConfig.LogConfig}}'
```

预期日志要点：

- `NoteService + ChromaDB 初始化成功`
- `重排序模型初始化失败/超时` 的 warning —— **这是预期行为**（见 §2.1），不是故障
- 数据库迁移无报错
- 宿主机 `logs/app.log` 有内容持续写入（说明属主修正生效）


**功能验证清单**（浏览器访问 `http://服务器IP`）：

1. ✅ 首页加载、登录/注册（如配了 SMTP 则验证邮件验证码）
2. ✅ 发起一条对话，确认**流式逐字输出**正常（验证 SSE 未被代理层缓冲）
3. ✅ 上传一个 PDF 到知识库，提问验证 RAG 检索召回
4. ✅ 笔记创建/编辑/搜索
5. ✅（如配了 Tavily）触发一次联网搜索工具调用

---

## 10. 端口放行

两处都要放行，缺一不可：

1. **宝塔面板**：「安全」→ 添加放行端口 `80`
2. **云服务商控制台**：安全组/防火墙规则 → 入方向放行 TCP `80`（以及 SSH `22`）

> 高频踩坑：宝塔放行了但云安全组没放行，表现为服务器本机 curl 通、外网打不开。

---

## 11. 上线后运维

### 11.1 每日备份（宝塔计划任务或 crontab）

```bash
mkdir -p /opt/backup
# 写入 /opt/backup.sh 并 chmod +x
#!/bin/bash
DATE=$(date +%F)
# MySQL 逻辑备份
docker exec raglearn-mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" raglearn' \
  > /opt/backup/db_${DATE}.sql 2>/dev/null
# 数据目录备份（ChromaDB 向量库 + 上传文件）
tar czf /opt/backup/data_${DATE}.tar.gz /opt/RagLearnCode/data
# 保留最近 14 天
find /opt/backup -name "*.tar.gz" -mtime +14 -delete
find /opt/backup -name "*.sql" -mtime +14 -delete
```

crontab：`0 3 * * * /opt/backup.sh`

> `./data` 目录是 MySQL 之外唯一必须备份的目录（ChromaDB 向量库 + 上传文件 + 日志）。仅靠 `docker volume` 里的 mysql_data 卷不算备份。

### 11.2 资源监控

```bash
docker stats                                    # 实时容器资源
free -h && swapon --show                        # 内存与 swap
df -h /opt                                      # 磁盘
```

可选：MySQL 内存收紧（编辑 compose 的 mysql command 追加参数）：

```yaml
command: --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci --innodb-buffer-pool-size=128M
```

### 11.3 代码更新流程

```bash
cd /opt/RagLearnCode
git pull
docker compose up -d --build backend frontend    # 只重建有代码变更的服务
docker compose logs -f backend                   # 观察迁移与新版本启动
```

> 更新前建议先执行一次 11.1 的备份，便于回滚（`git checkout <旧tag>` 后重建）。

补充说明：

- 若本次更新涉及 `Dockerfile`、`docker-compose.yml`、`front/Dockerfile`、`front/.dockerignore`，同样用上面的命令重建即可；改动是向下兼容的，`data/`、`logs/` 数据不受影响
- 若更新涉及 `.env` 中的**构建类**变量（`NGINX_CONF` / `PIP_INDEX_URL` / `NPM_REGISTRY` / `PRELOAD_MODELS`），必须带 `--build` 重建对应服务，单纯 `restart` 不生效
- 数据目录属主由入口脚本每次启动自动修正，`git pull` 后即便目录属主被重置也无需手工处理（见 §6.2）
- 方案 A（本地构建传输）用户：`docker compose build backend frontend` 后 `docker save raglearn-backend raglearn-front | gzip > raglearn-images.tar.gz`；compose 已显式声明这两个镜像名，命令可直接使用


---

## 12. 域名备案后切换 HTTPS

前置条件：域名已购买、备案通过、DNS A 记录指向服务器 IP。

1. **申请证书**：宝塔「网站 → SSL → Let's Encrypt」申请，或 `certbot certonly --standalone -d 你的域名`；将证书复制到项目目录：
   ```bash
   mkdir -p /opt/RagLearnCode/certs
   cp fullchain.pem privkey.pem /opt/RagLearnCode/certs/
   ```
2. **切回 HTTPS 配置**：把 `.env` 中的 `NGINX_CONF` 改回 `nginx.conf`（或直接删掉这一行，使用默认值），**无需手工改动任何 nginx 文件**
   > `front/nginx.conf` 的 `server_name` 为 `_`（接受任意 Host），裸域/域名均可正常工作；如需严格限制可自行改为你的域名
3. **更新 .env**：`CORS_ORIGINS=https://你的域名`
4. **重建前端**：
   ```bash
   docker compose up -d --build frontend
   ```
5. 验证：`https://你的域名` 正常访问 + 浏览器地址栏锁标志 + HTTP 自动跳转 HTTPS

---

## 13. 常见问题排查

| 现象 | 排查方向 |
|------|---------|
| 浏览器打不开，服务器本机 `curl localhost` 正常 | 云服务商安全组未放行 80（见 §10） |
| frontend 容器反复重启 | 镜像内的 nginx 配置是 HTTPS 版但 `./certs` 无证书 → 在 **构建机** `.env` 设 `NGINX_CONF=nginx.http.conf` 后重建前端（见 §7、§8） |
| backend 容器反复重启，日志含 `logs/app.log` 权限错误 | 入口脚本未生效（旧镜像）。v1.1 起由 `scripts/docker-entrypoint.sh` 自动修正属主，重建镜像即可；确认 `docker compose logs backend` 开头有 `[entrypoint]` 相关输出（见 §6.2） |
| backend 容器 unhealthy 超过 3 分钟 | `docker compose logs backend` 看启动错误；常见：JWT_SECRET 为空、MySQL 未就绪、迁移失败 |
| 对话无流式效果（一次性蹦出全文） | 代理层缓冲问题。确认使用的是容器自带 nginx（已配 `proxy_buffering off`）；若自行改用了宝塔 nginx 反代，需在对应 location 加该配置 |
| 容器被杀，`dmesg` 见 OOM Killer | swap 未生效（`free -h` 检查）或误加载了重排序模型（检查 §2.1 两条红线） |
| 前端构建失败 Killed | 服务器内存不足，改用 §8 方案 A 本地构建 |
| 后端构建时 pip 卡住 / 超时 | 未配置国内镜像源，见 §6.1 设置 `PIP_INDEX_URL` |
| 前端构建时 npm 卡住 / 超时 | 未配置国内镜像源，见 §6.1 设置 `NPM_REGISTRY` |
| `重排序模型初始化失败` warning | **2G 服务器属预期行为**（§2.1）。若已扩容到 4G+ 仍报此错，检查缓存是否放在 `./huggingface_cache`（挂载点 `/opt/hf-cache`，见 §2.1 修正说明） |
| 后端日志出现 `api.openai.com` 连接超时 | 国内服务器直连不可用，需按 §14 配置出网代理 |
| git clone 超时 | GitHub 直连受限，用 §5 的代理/镜像方式 |
| 拉取镜像超时 | 检查 §4 第 2 步镜像加速配置是否生效（`docker info` 看 Registry Mirrors） |

---

## 14. 出网代理（OpenAI 等外网模型）

只有当后端需要访问境内无法直连的海外 API（典型是 `api.openai.com`）时才需要本章。当前 `MODEL_PROVIDER=dashscope`（阿里云百炼，境内可直连）**不需要**配置。

### 14.1 原理

`app/utils/factory.py` 创建 `httpx.AsyncClient` 时保留了 `trust_env` 的默认值（`True`），httpx 会自动读取进程环境变量中的 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`，**代码无需任何改动**。

而 compose 的 backend 服务使用 `env_file: - .env`，因此只要把代理写进 `.env`，就会自动透传进容器并生效。

### 14.2 配置

在 `.env` 中追加（`NO_PROXY` 必须包含内部服务名，否则 MySQL / Redis 的内网连接也会被送进代理）：

```ini
HTTPS_PROXY=http://172.17.0.1:7890
HTTP_PROXY=http://172.17.0.1:7890
NO_PROXY=localhost,127.0.0.1,mysql,redis,backend,frontend
```

### 14.3 代理地址怎么写（关键易错点）

**容器内的 `127.0.0.1` 指的是容器自己，不是宿主机**：

| 代理部署位置 | 应填写的地址 |
|-------------|-------------|
| 宿主机（Linux） | `http://172.17.0.1:<port>`（docker 默认网桥网关）或宿主机内网 IP，**不要写 127.0.0.1** |
| 宿主机（Docker Desktop / Win / Mac） | `http://host.docker.internal:<port>` |
| 另一个 compose 服务 | `http://<服务名>:<port>` |

`172.17.0.1` 可用 `ip -4 addr show docker0` 确认；若 Docker 使用了其他网段，以实际网关地址为准。

### 14.4 验证与影响

```bash
# 1. 确认代理变量已进入容器
docker exec raglearn-backend env | grep -i proxy

# 2. 容器内连通性测试（配置正确时应返回 401/200 等 HTTP 状态码，而非超时）
docker exec raglearn-backend python -c "import httpx; print(httpx.get('https://api.openai.com/v1/models', timeout=15).status_code)"

# 3. 确认内网服务未被代理
docker exec raglearn-backend python -c "import socket; print(socket.gethostbyname('mysql'))"
```

注意：

- 该代理对容器内**所有** HTTPS 出网请求生效，包括原有的百炼（DashScope）调用。若只想让部分流量走代理，建议改用「中转网关」方案——把 `OPENAI_BASE_URL` 指向可达的网关地址，而不设置全局代理（详见 `docs/DashScope迁移OpenAI大模型接入设计方案.md` §5.8）
- 代理凭据（形如 `http://user:pass@host:port`）会以明文出现在容器环境变量中，`docker inspect` 可见，注意宿主机访问权限
- 修改 `.env` 后需重启 backend：`docker compose up -d backend`

---

## 附录：部署前检查清单

- [ ] 服务器已安装 Docker 并配置镜像加速
- [ ] 已创建 4G swap 且 `/etc/fstab` 持久化
- [ ] 宝塔自带 nginx 已停止/卸载（释放 80 端口）
- [ ] 代码已 clone 到 `/opt/RagLearnCode`
- [ ] `.env` 已配置：`MODEL_PROVIDER=dashscope`、`DASHSCOPE_API_KEY`、`MYSQL_PASSWORD`、`JWT_SECRET`、`APP_ENV=production`、`LOG_LEVEL=INFO`、`LOG_FORMAT=json`
- [ ] 无域名阶段：`.env` 已设 `NGINX_CONF=nginx.http.conf`（**方案 A 时构建机上也要设**，见 §8）
- [ ] 服务器直接构建时：`.env` 已设 `PIP_INDEX_URL` / `NPM_REGISTRY`（见 §6.1）
- [ ] 未设置 `PRELOAD_MODELS=true`，`huggingface_cache` 目录为空（见 §2.1）
- [ ] 云安全组 + 宝塔均放行 80 端口
- [ ] 完成 §9 验证：容器 healthy、`docker exec raglearn-backend id` 显示 `uid=1000(appuser)`、日志目录可写探针通过
- [ ] 浏览器完成 §9 功能验证清单（重点：流式输出）
- [ ] 每日备份任务已配置
- [ ]（如需访问 OpenAI）已按 §14 配置 `HTTPS_PROXY` 并验证连通

