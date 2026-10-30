# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS web
WORKDIR /build/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY web/ ./
RUN pnpm run build

FROM python:3.12-slim-bookworm AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home whale \
    && install -d -o whale -g whale -m 700 /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY --from=web /build/web/dist/ ./src/whale_core/web_dist/
RUN python -m pip install --no-cache-dir ".[voice]"
USER whale
EXPOSE 8080 8081
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import socket;s=socket.create_connection(('127.0.0.1',8080),3);s.close()"
ENTRYPOINT ["whale", "api", "--config-dir", "/data", "--host", "0.0.0.0", "--port", "8080"]
