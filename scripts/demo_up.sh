#!/usr/bin/env bash
# 一条命令准备演示环境（答辩机用）。
#
#   bash scripts/demo_up.sh              # 准备数据并起全套（后端 8000 + worker + 前端 5173）
#   bash scripts/demo_up.sh --fresh      # 空库重建：删库 → 迁移 → 种子 → 冒烟，然后退出
#   bash scripts/demo_up.sh --no-serve   # 只准备数据（迁移 + 种子 + 冒烟），不启动服务
#   bash scripts/demo_up.sh --serve      # 配合 --fresh：重建完继续起服务
#   bash scripts/demo_up.sh --smoke      # 起服务前先跑一次端到端冒烟
#
# 默认库是 services/api/demo.db（SQLite）。换库：
#   bash scripts/demo_up.sh --db-url "postgresql+psycopg://user:pass@localhost/culturalwalk"
#
# 为什么是脚本而不是文档里的一串命令：答辩机只装了 Git Bash，没有 docker，
# 也不可能让人现场敲四条命令还记得顺序。这里把顺序、库地址、venv 和 pnpm 的
# 查找都固化下来，出错时给的是「哪一步失败」而不是一句 traceback。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_URL="sqlite:///./demo.db"
FRESH=0
SERVE=1
SERVE_EXPLICIT=0
SMOKE=0
API_PORT="${API_PORT:-8000}"
# 前端固定 5173：apps/web/scripts/dev-real.mjs 把端口写死在里面，同时负责把
# VITE_USE_MOCK=false 传给 vite。用普通 `pnpm dev` 会走假数据，登录页连演示账号都对不上。
WEB_PORT=5173

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while (( $# > 0 )); do
  case "$1" in
    --fresh) FRESH=1 ;;
    --no-serve) SERVE=0; SERVE_EXPLICIT=1 ;;
    --serve) SERVE=1; SERVE_EXPLICIT=1 ;;
    --smoke) SMOKE=1 ;;
    --db-url) DB_URL="${2:?--db-url 后面要跟数据库地址}"; shift ;;
    --api-port) API_PORT="${2:?--api-port 后面要跟端口}"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n== %s\n' "$1"; }
die() { printf '失败：%s\n' "$1" >&2; exit 1; }

# ---- 找 python：venv 优先，别用 Windows 商店占位 ---------------------------
PY_BIN=""
for cand in "$ROOT/services/api/.venv/Scripts/python.exe" "$ROOT/services/api/.venv/bin/python"; do
  [ -x "$cand" ] && PY_BIN="$cand" && break
done
if [ -z "$PY_BIN" ]; then
  PY_BIN="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"
fi
[ -n "$PY_BIN" ] || die "找不到 python。先建虚拟环境：cd services/api && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt"
echo "python: $PY_BIN"

# ---- 找 pnpm / node（Git Bash 常常两者都不在 PATH 里）---------------------
NODE_BIN="$(command -v node 2>/dev/null || true)"
PNPM_CMD=()
if command -v pnpm >/dev/null 2>&1; then
  PNPM_CMD=(pnpm)
else
  for cjs in \
    "$HOME/AppData/Local/pnpm/pnpm.cjs" \
    "$HOME/AppData/Local/pnpm/bin/pnpm.cjs" \
    "$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.cjs"
  do
    if [ -f "$cjs" ]; then
      if [ -z "$NODE_BIN" ]; then
        for n in "$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe" \
                 "/c/Program Files/nodejs/node.exe"; do
          [ -x "$n" ] && NODE_BIN="$n" && break
        done
      fi
      if [ -n "$NODE_BIN" ]; then
        PNPM_CMD=("$NODE_BIN" "$cjs")
        break
      fi
    fi
  done
fi

# pnpm 跑包脚本时还会再调用 node；Git Bash 里 node 常常不在 PATH，补上，
# 否则前端会报 "'node' is not recognized"（vite 的 .cmd 垫片需要 node）。
if [ -n "$NODE_BIN" ] && ! command -v node >/dev/null 2>&1; then
  export PATH="$(dirname "$NODE_BIN"):$PATH"
fi

# --fresh 是自测/重建用的：默认重建完就退出（否则会一直挂着等 Ctrl-C）；
# 想重建完继续起服务就再加 --serve。
if (( FRESH && ! SERVE_EXPLICIT )); then
  SERVE=0
fi

# ---- 空库：只处理 SQLite 文件；换 PG 请自己在库端清 ------------------------
if (( FRESH )); then
  case "$DB_URL" in
    sqlite:///*)
      db_file="${DB_URL#sqlite:///}"
      db_file="${db_file#./}"
      target="$ROOT/services/api/$db_file"
      if [ -f "$target" ]; then
        say "删掉旧库 $target"
        rm -f "$target" || die "删不掉 $target"
      fi
      ;;
    *) echo "警告：--fresh 只会删 SQLite 文件；$DB_URL 需要你自己清库" ;;
  esac
fi

# ---- 迁移 + 种子 -----------------------------------------------------------
say "迁移到最新版本（alembic upgrade head）"
(
  cd "$ROOT/services/api" || exit 1
  DATABASE_URL="$DB_URL" "$PY_BIN" -m alembic upgrade head
) || die "alembic upgrade head 失败（看上面最后一条错误）"

say "灌种子（10 条已发布路线 + 3 条预生成行程）"
# 注意：sqlite 的相对路径是相对当前工作目录解析的，这里必须和 alembic/uvicorn
# 一样在 services/api 下跑，否则会在仓库根另建一个空库（种子会报 no such table）。
(
  cd "$ROOT/services/api" || exit 1
  "$PY_BIN" "$ROOT/scripts/seed_demo.py" --database-url "$DB_URL"
) || die "种子脚本失败"

# ---- 冒烟（可选）----------------------------------------------------------
if (( SMOKE || FRESH )); then
  say "端到端冒烟（注册 → 草稿 → 节点 → 详情 → 删除）"
  (
    cd "$ROOT/services/api" || exit 1
    "$PY_BIN" "$ROOT/scripts/e2e_smoke.py" --database-url "$DB_URL"
  ) || die "冒烟失败"
fi

if (( ! SERVE )); then
  say "数据已就绪（--no-serve，未启动服务）"
  echo "后端：cd services/api && DATABASE_URL=\"$DB_URL\" $PY_BIN -m uvicorn app.main:app --port $API_PORT"
  echo "前端：pnpm -C apps/web dev:real（真接口模式；普通 dev 会走假数据）"
  exit 0
fi

# ---- 起全套 ---------------------------------------------------------------
(( ${#PNPM_CMD[@]} > 0 )) || die "找不到 pnpm/node，起不了前端。装 pnpm 或手动指定 PATH 后重试。"

PIDS=()
cleanup() {
  printf '\n== 收场\n'
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

say "启动后端 :$API_PORT"
(
  cd "$ROOT/services/api" || exit 1
  DATABASE_URL="$DB_URL" exec "$PY_BIN" -m uvicorn app.main:app --port "$API_PORT"
) &
PIDS+=("$!")

say "启动规划 worker"
(
  cd "$ROOT/services/api" || exit 1
  DATABASE_URL="$DB_URL" exec "$PY_BIN" -m app.modules.planner.worker
) &
PIDS+=("$!")

say "启动前端 :$WEB_PORT（dev:real，走真接口）"
(
  cd "$ROOT" || exit 1
  exec "${PNPM_CMD[@]}" -C apps/web dev:real
) &
PIDS+=("$!")

cat <<EOF

== 就绪
  前端    http://localhost:$WEB_PORT
  后端    http://127.0.0.1:$API_PORT/docs
  账号    demo@demo.local / editor@demo.local / reviewer@demo.local
  密码    Demo12345!

Ctrl-C 一起停掉三个进程。
EOF

wait
