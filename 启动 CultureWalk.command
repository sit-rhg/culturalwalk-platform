#!/bin/bash
set -e

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -x services/api/.venv/bin/python ]; then
  PYTHON="$(command -v python3.12 || command -v python3.13 || command -v python3.14 || true)"
  if [ -z "$PYTHON" ]; then
    printf '未找到 Python 3.12 或更高版本，请先安装 Python：https://www.python.org/downloads/\n' >&2
    exit 1
  fi
  printf '首次运行：创建 Python 虚拟环境...\n'
  "$PYTHON" -m venv services/api/.venv
fi

if ! services/api/.venv/bin/python -c 'import alembic, fastapi, uvicorn' >/dev/null 2>&1; then
  printf '首次运行：安装后端依赖...\n'
  services/api/.venv/bin/python -m pip install -e "./services/api[dev]"
fi

if [ ! -x apps/web/node_modules/.bin/vite ]; then
  if ! command -v pnpm >/dev/null 2>&1; then
    printf '未找到 pnpm，请先安装 Node.js 20+ 和 pnpm。\n' >&2
    exit 1
  fi
  printf '首次运行：安装前端依赖...\n'
  pnpm -C apps/web install
fi

exec bash scripts/demo_up.sh "$@"
