# ===== Vue production build =====
FROM node:20-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ===== Python 运行镜像 =====
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 系统依赖：tzdata + psycopg2-binary 运行时所需的 libpq、curl 用于 healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /app

# 先装依赖以便利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝源码
COPY config/ ./config/
COPY internal/ ./internal/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY main.py ./
COPY --from=web-build /web/dist ./web/dist/

ENV FRONTEND_DIR=/app/web/dist \
    AGI_CONFIG=/app/config/config.yaml \
    AGI_EVAL_DATABASE_URL=sqlite:////app/runtime/application.db \
    AGI_AUTH_REQUIRED=1

# The service never needs to modify its code or bundled configuration.  Keep
# the only writable tree below /app/runtime, including the system user's home
# so '~' based artifact paths cannot escape into another writable location.
RUN addgroup --system --gid 10001 agi \
    && adduser --system --uid 10001 --ingroup agi \
        --home /app/runtime/home/agi --no-create-home --disabled-password agi \
    && mkdir -p /app/runtime/home/agi/Desktop/agi-saber \
    && chmod -R a-w /app/config /app/internal /app/alembic /app/web \
        /app/main.py /app/alembic.ini /app/requirements.txt \
    && chown -R agi:agi /app/runtime \
    && chmod -R u+rwX,go-rwx /app/runtime

USER 10001:10001

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8090/health || exit 1

ENTRYPOINT ["python", "-u", "main.py"]
