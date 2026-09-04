# 宝塔 Linux 服务器 Docker 部署方案

> 版本：v1.0（2026-08-22）
> 适用环境：宝塔 Linux 面板（uxue 服务器，2核 2G 内存），几十人规模使用，暂无域名/未备案
> 部署方式：Docker Compose（项目已完整容器化：mysql + redis + backend + frontend/nginx）

---

## 目录

1. [部署方式选型：为什么用 Docker](#1-部署方式选型为什么用-docker)
2. [2核2G 服务器的三个现实约束](#2-2核2g-服务器的三个现实约束)
3. [无域名阶段的处理](#3-无域名阶段的处理)
4. [服务器环境准备](#4-服务器环境准备)
5. [获取代码](#5-获取代码)
6. [配置 .env](#6-配置-env)
7. [修改前端 nginx.conf（临时 HTTP 版）](#7-修改前端-nginxconf临时-http-版)
8. [构建与启动](#8-构建与启动)
9. [验证部署](#9-验证部署)
10. [端口放行](#10-端口放行)
11. [上线后运维（备份/监控/更新）](#11-上线后运维备份监控更新)
12. [域名备案后切换 HTTPS](#12-域名备案后切换-https)
13. [常见问题排查](#13-常见问题排查)

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

项目现状（已核查）：

- `docker-compose.yml`：四服务编排，含健康检查、依赖顺序（backend 等 mysql/redis healthy）、`restart: unless-stopped`
- `Dockerfile`：多阶段构建，启动前自动执行 `alembic upgrade head` 数据库迁移，非 root 运行，`/health` 健康探针
- `front/Dockerfile`：node:22 构建 + nginx:1.27 托管，SPA 路由回退已配
- 注意：compose 注释中提到的 `docker-compose.prod.yml` **实际不存在**，生产覆盖通过 `.env` 中 `APP_ENV=production` 实现即可

**宝塔面板的角色**：只用它的端口放行（安全）、资源监控、计划任务（备份）功能。**不要**用宝塔的"网站/nginx"功能去反代本项目——前端容器自带 nginx 且占用 80 端口，宝塔 nginx 会与之冲突。

---

## 2. 2核2G 服务器的三个现实约束

### 2.1 重排序模型装不下 —— 会自动降级，无需处理

`main.py` 启动时（阶段 3）会加载 CrossEncoder 重排序模型 `BAAI/bge-reranker-v2-m3`（配置于 `config/chroma.yaml`），该模型 fp32 加载需要 **2GB+ 内存**，本服务器装不下。

**代码已有优雅降级**：运行镜像强制 `HF_HUB_OFFLINE=1`，模型不在本地缓存时 `_init_reranker` 捕获异常、仅记录 warning 后跳过（`main.py:179-198`）。RAG 检索流程照常工作，只是没有重排序打分，几十人使用场景质量损失可接受。

**因此部署时注意两条红线**：

1. 构建后端镜像**不要**使用 `--build-arg PRELOAD_MODELS=true`（会把 ~1GB 模型打进镜像并在运行时吃满内存）
2. **不要**向 `huggingface_cache` 卷放入任何模型缓存（compose 第 61 行的挂载，部署时留空目录即可）

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

`front/nginx.conf` 当前**强制 HTTPS**：

- 80 端口 `return 301 https://...` 全部跳转
- 443 端口 `ssl_certificate` 指向 `/etc/nginx/certs/` 下的证书，**不挂载证书 nginx 启动直接失败**

而 Let's Encrypt 不为裸 IP 签发证书，所以**没有域名时必须临时改为纯 HTTP**（改法见 §7）。

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

**按需配置**（不配则对应功能返回"未配置"提示，不影响主流程）：

- `SMTP_*`：注册/登录邮件验证码（QQ 邮箱授权码，非登录密码）
- `TAVILY_API_KEY`：Agent 联网搜索工具
- `DEEPL_API_KEY` / `WOLFRAM_APP_ID`：翻译/计算工具

**保持默认即可**：`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`UVICORN_RELOAD=false`、`PPT_ENGINE=python_pptx`。

---

## 7. 修改前端 nginx.conf（临时 HTTP 版）

无域名阶段，将 `front/nginx.conf` 整体替换为以下内容（改动点：删除 301 跳转和 443 server 块、去掉 SSL/HSTS 配置；**/api/ 的 `proxy_buffering off` 等 SSE 配置必须原样保留**）：

```nginx
# RAG LearnLittle 前端 nginx 配置（临时 HTTP 版，无域名/IP 直连阶段）
# 域名备案完成后请恢复 HTTPS 版本（见 docs 部署方案 §12）

server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    # 前端静态资源 + SPA 路由回退
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反代（SSE 禁用缓冲 —— 不可删除，否则对话流式输出会被缓冲卡住）
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    # 静态头像
    location /static/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
    }

    # 安全响应头（HSTS 暂不加 —— HTTP 阶段加 HSTS 无意义）
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
```

同时确认 `docker-compose.yml` 前端服务的端口映射只保留 `"80:80"`（443 映射留着也无害，但临时阶段用不到证书卷）。

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

### 首次启动行为说明

- backend 容器启动命令为 `alembic upgrade head && uvicorn ...`，会先执行数据库迁移建表，**健康检查有 120s 宽限期**（`start-period=120s`），期间 `docker compose ps` 显示 unhealthy 属正常
- 首次启动 mysql 容器初始化约需 30 秒，backend 会等待其 healthy 后才开始

---

## 9. 验证部署

```bash
# 1. 容器状态：四个容器均应 running/healthy
docker compose ps

# 2. 后端启动日志（观察关键初始化阶段）
docker compose logs backend | grep -E "初始化|启动|ERROR|WARNING"

# 3. 容器内健康探针
docker exec raglearn-backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status)"
```

预期日志要点：

- `NoteService + ChromaDB 初始化成功`
- `重排序模型初始化失败/超时` 的 warning —— **这是预期行为**（见 §2.1），不是故障
- 数据库迁移无报错

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

---

## 12. 域名备案后切换 HTTPS

前置条件：域名已购买、备案通过、DNS A 记录指向服务器 IP。

1. **申请证书**：宝塔「网站 → SSL → Let's Encrypt」申请，或 `certbot certonly --standalone -d 你的域名`；将证书复制到项目目录：
   ```bash
   mkdir -p /opt/RagLearnCode/certs
   cp fullchain.pem privkey.pem /opt/RagLearnCode/certs/
   ```
2. **恢复 nginx.conf**：把 §7 的临时 HTTP 版替换回仓库原版（80 跳转 301 + 443 SSL server 块），`server_name` 改为你的域名
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
| frontend 容器反复重启 | nginx.conf 仍是 HTTPS 版且 certs/ 无证书 → 按 §7 改 HTTP 版 |
| backend 容器 unhealthy 超过 3 分钟 | `docker compose logs backend` 看启动错误；常见：JWT_SECRET 为空、MySQL 未就绪、迁移失败 |
| 对话无流式效果（一次性蹦出全文） | 代理层缓冲问题。确认使用的是容器自带 nginx（已配 `proxy_buffering off`）；若自行改用了宝塔 nginx 反代，需在对应 location 加该配置 |
| 容器被杀，`dmesg` 见 OOM Killer | swap 未生效（`free -h` 检查）或误加载了重排序模型（检查 §2.1 两条红线） |
| 前端构建失败 Killed | 服务器内存不足，改用 §8 方案 A 本地构建 |
| `重排序模型初始化失败` warning | **预期行为**（§2.1），不影响主流程 |
| git clone 超时 | GitHub 直连受限，用 §5 的代理/镜像方式 |
| 拉取镜像超时 | 检查 §4 第 2 步镜像加速配置是否生效（`docker info` 看 Registry Mirrors） |

---

## 附录：部署前检查清单

- [ ] 服务器已安装 Docker 并配置镜像加速
- [ ] 已创建 4G swap 且 `/etc/fstab` 持久化
- [ ] 宝塔自带 nginx 已停止/卸载（释放 80 端口）
- [ ] 代码已 clone 到 `/opt/RagLearnCode`
- [ ] `.env` 已配置：`MODEL_PROVIDER=dashscope`、`DASHSCOPE_API_KEY`、`MYSQL_PASSWORD`、`JWT_SECRET`、`APP_ENV=production`
- [ ] `front/nginx.conf` 已改为临时 HTTP 版（保留 SSE 配置）
- [ ] 未使用 `PRELOAD_MODELS=true` 构建，`huggingface_cache` 目录为空
- [ ] 云安全组 + 宝塔均放行 80 端口
- [ ] 浏览器完成 §9 功能验证清单（重点：流式输出）
- [ ] 每日备份任务已配置
