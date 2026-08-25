# 前端镜像。构建产静态文件，nginx 提供。
#
# 不用 node 跑生产服务：SPA 构建后就是静态资源，nginx 提供它比 Node 进程省一个数量级的
# 内存，也少一层需要维护的运行时。

FROM node:24-alpine AS builder

WORKDIR /build

# 先只复制依赖清单，让 npm ci 的层能被缓存——改前端代码不必重装依赖。
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# tsc -b && vite build。类型错误会让构建失败，这是有意的——带类型错误的产物不该进镜像。
RUN npm run build

FROM nginx:1.29-alpine

COPY --from=builder /build/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1/ || exit 1
