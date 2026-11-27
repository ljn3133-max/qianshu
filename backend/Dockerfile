FROM python:3.11-slim

WORKDIR /app

ENV TZ=Asia/Shanghai

# 数据库备份依赖：compose 的 PostgreSQL 服务为 16，pg_dump 主版本必须不低于服务端。
# Debian bookworm 自带客户端是 15，故从 PGDG 装 16；APT 源按 华为 -> 阿里云 -> 官方
# 依次回退，受限网络可用 --build-arg PGDG_APT_BASE 指定内网镜像（设为空禁用该轮）。
ARG PGDG_APT_BASE=https://mirrors.huaweicloud.com/postgresql/repos/apt
ARG PGDG_APT_FALLBACK="https://mirrors.aliyun.com/postgresql/repos/apt https://apt.postgresql.org/pub/repos/apt"
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    ok=0; \
    for base in "$PGDG_APT_BASE" $PGDG_APT_FALLBACK; do \
      [ -n "$base" ] || continue; \
      echo "pgdg apt via $base"; \
      mkdir -p /usr/share/postgresql-common/pgdg; \
      if curl -fsSL --retry 2 -m 30 "$base/ACCC4CF8.asc" \
           -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
         && printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] %s bookworm-pgdg main\n' "$base" \
              > /etc/apt/sources.list.d/pgdg.list \
         && apt-get update \
         && apt-get install -y --no-install-recommends postgresql-client-16; then \
        ok=1; break; \
      fi; \
      echo "pgdg apt failed, trying next: $base" >&2; \
    done; \
    rm -rf /var/lib/apt/lists/*; \
    [ "$ok" = "1" ]; \
    pg_dump --version

# 离线镜像形态：构建上下文里带 vendor/（与源码离线包同一套 wheel）时零外网安装，
# 版本与源码形态完全一致；装完立刻删除，vendor 不进入镜像层。
# 在线构建：无 vendor 时先走 PIP_INDEX，失败后按 PIP_FALLBACK_INDEX 依次回退。
# 镜像站偶发 403/缺包时不能让整次安装失败；内网可把回退列表设为空，强制只用指定源。
COPY . .
ARG PIP_INDEX=https://pypi.org/simple
ARG PIP_FALLBACK_INDEX="https://mirrors.huaweicloud.com/repository/pypi/simple https://mirrors.aliyun.com/pypi/simple https://pypi.org/simple"
RUN if [ -d vendor ]; then \
      pip install --no-cache-dir --no-index --find-links vendor -r requirements.txt \
      && rm -rf vendor; \
    else \
      ok=0; \
      for idx in "$PIP_INDEX" $PIP_FALLBACK_INDEX; do \
        [ -n "$idx" ] || continue; \
        echo "pip install via $idx"; \
        if pip install --no-cache-dir --retries 2 --timeout 30 -r requirements.txt -i "$idx"; then \
          ok=1; break; \
        fi; \
        echo "pip index failed, trying next: $idx" >&2; \
      done; \
      if [ "$ok" != "1" ]; then \
        printf 'PIP_INSTALL_ALL_%s_FAILED\n' INDEXES >&2; \
      fi; \
      [ "$ok" = "1" ]; \
    fi

EXPOSE 8810
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8810"]
