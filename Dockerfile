# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS console
WORKDIR /build/web
RUN npm install --global pnpm@12.8.1
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY web/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm AS wheel
WORKDIR /build
COPY pyproject.toml README.md README_EN.md CONTRIBUTING.md ./
COPY src/ ./src/
COPY licenses/ ./licenses/
COPY docs/ ./docs/
COPY --from=console /build/web/dist/ ./src/whale_core/static/
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# bubblewrap is optional at runtime: restricted hosts must fail closed, never
# silently fall back to unsandboxed shell execution. No privileged container.
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 whale \
    && useradd --uid 10001 --gid whale --create-home whale \
    && mkdir -p /data/whale /opt/whale \
    && chown whale:whale /data/whale
COPY --from=wheel /wheels/ /wheels/
RUN for wheel_file in /wheels/*.whl; do python -m pip install --no-cache-dir "${wheel_file}[voice]"; done \
    && rm -rf /wheels
COPY scripts/container_healthcheck.py /opt/whale/healthcheck.py
USER whale:whale
WORKDIR /home/whale
VOLUME ["/data/whale"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=8s --start-period=15s --retries=3 \
    CMD ["python", "/opt/whale/healthcheck.py"]
CMD ["whale", "api", "--host", "0.0.0.0", "--port", "8080", "--config-dir", "/data/whale"]
