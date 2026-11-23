# 鲸鱼 Whale —— 适合 NAS / 家庭服务器 / 云主机
#   docker compose up -d     然后用浏览器打开 http://<机器地址>:8080 ，令牌见 `docker compose logs whale`
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WHALE_HOME=/data TZ=Asia/Shanghai
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
COPY contracts ./contracts
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 whale && mkdir -p /data && chown whale /data
USER whale
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')"
CMD ["whale", "serve", "--host", "0.0.0.0", "--port", "8080", "--no-open"]
