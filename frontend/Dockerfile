FROM node:20-alpine AS build
WORKDIR /app
# npm 源可换（compose build.args NPM_REGISTRY / --build-arg）；空=默认源
ARG NPM_REGISTRY=""
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund   # lock 锁定版本（构建复现性）
COPY . .
# 离线包形态 frontend/ 只带预构建 dist/（无 src/）：跳过构建直接用 dist；
# 源码形态照旧 npm run build（npm ci 层在上面，缓存不受源码改动影响）
RUN if [ -d src ]; then npm run build; else echo "prebuilt dist, skip npm build"; fi

# 固定 1.29：1.31 起 pid 文件写入用 pwritev，CentOS 7 (kernel 3.10) + Docker 内置
# seccomp 下 EPERM 启动失败（2026-11-12 8.31 实测）。升级前需回归老内核。
FROM nginx:1.29-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template
# 入口先从容器 resolv.conf 发现 DNS（docker/podman 通吃），再走官方 envsubst 模板渲染
CMD ["/bin/sh", "-c", "export DNS_RESOLVER=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf); exec /docker-entrypoint.sh nginx -g 'daemon off;'"]
EXPOSE 80
