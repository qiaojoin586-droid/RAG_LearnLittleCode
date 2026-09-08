# ========== RAG LearnLittle 前端镜像（构建 + nginx 托管） ==========
# 构建: docker build -t raglearn-front ./front
#
# 可选构建参数:
#   国内构建加速:   --build-arg NPM_REGISTRY=https://registry.npmmirror.com
#   无域名/无证书（纯 HTTP）: --build-arg NGINX_CONF=nginx.http.conf
#
# 说明: docker-compose.yml 已将 .env 中的 NPM_REGISTRY / NGINX_CONF 透传到这两个参数，
#       因此服务器部署通常只需改 .env，不必手写 docker build 命令。

# ---- 阶段 1: 构建 ----
FROM node:22-alpine AS build

WORKDIR /app

# npm 源：国内服务器构建建议改用镜像源，否则 npm ci 极慢/易超时
ARG NPM_REGISTRY=https://registry.npmjs.org/

# 利用 lock 文件缓存依赖层
COPY package.json package-lock.json ./
RUN npm ci --registry="$NPM_REGISTRY"

# front/.dockerignore 已排除 node_modules/dist，避免覆盖上面装好的依赖
COPY . .
# build = tsc 类型检查 + vite build（类型错误会在构建期暴露）
RUN npm run build

# ---- 阶段 2: nginx 托管 ----
FROM nginx:1.27-alpine

# nginx.conf（默认，强制 HTTPS，需 ./certs 下存在证书）
# nginx.http.conf（无域名/无证书阶段，纯 HTTP 直接可用）
ARG NGINX_CONF=nginx.conf

COPY --from=build /app/dist /usr/share/nginx/html
COPY ${NGINX_CONF} /etc/nginx/conf.d/default.conf

EXPOSE 80 443
