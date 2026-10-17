# 基础镜像固定版本 + digest，保证构建可复现（更新时请同时更新 tag 与 digest）
FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

LABEL org.opencontainers.image.title="trendradar" \
      org.opencontainers.image.source="https://github.com/LeilaoMi/hot-news-radar" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.description="TrendRadar 热点新闻聚合（hot-news-radar fork）"

WORKDIR /app

# Latest releases available at https://github.com/aptible/supercronic/releases
ARG TARGETARCH
ENV SUPERCRONIC_VERSION=v0.2.39

RUN set -ex && \
    apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    case ${TARGETARCH} in \
    amd64) \
    export SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64; \
    export SUPERCRONIC_SHA1SUM=c98bbf82c5f648aaac8708c182cc83046fe48423; \
    export SUPERCRONIC_SHA256SUM=ff32af9488866d5a3df7d65328ee94175c260d9e851d2cfb81d8764828543bee; \
    export SUPERCRONIC=supercronic-linux-amd64; \
    ;; \
    arm64) \
    export SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-arm64; \
    export SUPERCRONIC_SHA1SUM=5ef4ccc3d43f12d0f6c3763758bc37cc4e5af76e; \
    export SUPERCRONIC_SHA256SUM=7ec7456bb10368f1e7e0e0f92652dea8846806b16af0f37ccd45be95d258afd5; \
    export SUPERCRONIC=supercronic-linux-arm64; \
    ;; \
    *) \
    echo "Unsupported architecture: ${TARGETARCH}"; \
    exit 1; \
    ;; \
    esac && \
    echo "Downloading supercronic for ${TARGETARCH} from ${SUPERCRONIC_URL}" && \
    # 重试机制：最多3次，每次超时30秒
    for i in 1 2 3; do \
        echo "Download attempt $i/3"; \
        if curl -fsSL --connect-timeout 30 --max-time 60 -o "$SUPERCRONIC" "$SUPERCRONIC_URL"; then \
            echo "Download successful"; \
            break; \
        else \
            echo "Download attempt $i failed"; \
            if [ $i -eq 3 ]; then \
                echo "All download attempts failed"; \
                exit 1; \
            fi; \
            sleep 2; \
        fi; \
    done && \
    echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - && \
    echo "${SUPERCRONIC_SHA256SUM}  ${SUPERCRONIC}" | sha256sum -c - && \
    chmod +x "$SUPERCRONIC" && \
    mv "$SUPERCRONIC" "/usr/local/bin/${SUPERCRONIC}" && \
    ln -s "/usr/local/bin/${SUPERCRONIC}" /usr/local/bin/supercronic && \
    supercronic -version && \
    apt-get remove -y curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 从官方镜像拷贝 uv 二进制（固定版本 + digest）
COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /uvx /bin/

# 先安装依赖（利用 Docker 层缓存）
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# 再复制项目代码并安装项目本身
COPY docker/manage.py .
COPY trendradar/ ./trendradar/
COPY LICENSE ./LICENSE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# 复制 entrypoint.sh 并强制转换为 LF 格式
COPY docker/entrypoint.sh /entrypoint.sh.tmp
COPY docker/run-as-app.sh /usr/local/bin/run-as-app.tmp
RUN sed -i 's/\r$//' /entrypoint.sh.tmp /usr/local/bin/run-as-app.tmp && \
    mv /entrypoint.sh.tmp /entrypoint.sh && \
    mv /usr/local/bin/run-as-app.tmp /usr/local/bin/run-as-app && \
    chmod +x /entrypoint.sh /usr/local/bin/run-as-app && \
    chmod +x manage.py && \
    mkdir -p /app/config /app/output && \
    groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app && \
    chown -R app:app /app/output

ENV PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config/config.yaml \
    FREQUENCY_WORDS_PATH=/app/config/frequency_words.txt \
    APP_UID=10001 \
    APP_GID=10001

# 健康检查：cron 模式下 supercronic 必须是 PID 1 且 Web 服务器可访问；once 模式只检查进程存活
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "/app/manage.py", "healthcheck"]

# 入口以 root 启动，仅用于修正挂载卷 /app/output 的属主，随后立刻降权为 app（uid 10001）运行
# 也可以在 compose 里用 user: "10001:10001" 直接以非 root 启动（需自行保证 output 目录可写）
ENTRYPOINT ["/usr/local/bin/run-as-app", "/entrypoint.sh"]