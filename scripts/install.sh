#!/usr/bin/env bash
# 鲸鱼 Whale 一键安装（macOS / Linux）
#   curl -fsSL https://raw.githubusercontent.com/amwangfan/whale-core/main/scripts/install.sh | bash
# 再次运行即升级；数据（记忆、设置）保存在 ~/.whale，升级不会丢失。
set -euo pipefail

REF="${WHALE_REF:-main}"
APP_DIR="${WHALE_APP_DIR:-$HOME/.whale-app}"
SOURCE="https://github.com/amwangfan/whale-core/archive/refs/heads/${REF}.zip"
BIN_DIR="$HOME/.local/bin"

say() { printf '  \033[36m%s\033[0m\n' "$1"; }

PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  需要 Python 3.11 或更新版本。macOS 可用 'brew install python@3.12'，Ubuntu/Debian 可用 'sudo apt install python3.12 python3.12-venv'。"
  exit 1
fi

echo ""
echo "  🐋 安装鲸鱼 Whale"
mkdir -p "$APP_DIR" "$BIN_DIR"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  say "创建运行环境……"
  "$PY" -m venv "$APP_DIR/venv"
fi
say "下载并安装最新版本（约 1–3 分钟）……"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip -q
if "$APP_DIR/venv/bin/python" -m pip show whale-core >/dev/null 2>&1; then
  "$APP_DIR/venv/bin/python" -m pip install --upgrade --force-reinstall -q "whale-core @ $SOURCE"
else
  "$APP_DIR/venv/bin/python" -m pip install -q "whale-core @ $SOURCE"
fi
ln -sf "$APP_DIR/venv/bin/whale" "$BIN_DIR/whale"

if [ "$(uname)" = "Darwin" ]; then
  APP="$HOME/Applications/鲸鱼 Whale.command"
  mkdir -p "$HOME/Applications"
  printf '#!/bin/bash\nexec "%s/venv/bin/whale"\n' "$APP_DIR" > "$APP"
  chmod +x "$APP"
  say "已在「应用程序」(~/Applications) 放入「鲸鱼 Whale.command」，双击即可启动。"
fi

echo ""
say "安装完成！运行 'whale' 启动，浏览器会自动打开控制台。"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "（如果提示找不到 whale，请把 $BIN_DIR 加入 PATH，或直接运行 $BIN_DIR/whale）";; esac
echo ""
