# SPDX-License-Identifier: AGPL-3.0-or-later
# 前端镜像：构建产物是静态文件，运行时只需要 nginx
# 构建上下文是仓库根目录（见 compose.yaml 的 build.context）

FROM node:20-alpine AS build

WORKDIR /app

# 依赖层单独缓存：只改源码时不重装
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-fund --no-audit

COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------
FROM nginx:1.27-alpine

COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
