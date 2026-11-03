"""媒体扫描 worker：把 scan_state=scanning 的资产扫成 clean 或 rejected。

用法::

    python scripts/media_scan_worker.py --once
    python scripts/media_scan_worker.py --loop --interval 5 --limit 20

设计取舍：
- 单轮单事务：一批全提交；某个资产有问题只计数并跳过，不影响同批其他资产。
- 不退出循环：真实部署由 systemd/容器重启策略兜底，worker 自己只保证「下一轮还能跑」。
- 存储与判定都走模块层（get_object_store / MediaService.scan_pending），
  这里不写任何图片处理逻辑。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import SessionLocal  # noqa: E402
from app.modules.media.adapters.objects import get_object_store  # noqa: E402
from app.modules.media.service import MediaService  # noqa: E402


def run_once(limit: int) -> dict[str, int]:
    """扫一批并提交；返回 scanned/clean/rejected/skipped 计数。"""
    store = get_object_store()
    with SessionLocal() as session:
        counts = MediaService(session).scan_pending(store=store, limit=limit)
        session.commit()
    print(
        f"扫描 {counts['scanned']} 个资产："
        f"clean={counts['clean']}, rejected={counts['rejected']}, skipped={counts['skipped']}"
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="媒体扫描 worker")
    parser.add_argument("--once", action="store_true", help="只扫一轮就退出（默认）")
    parser.add_argument("--loop", action="store_true", help="常驻，按 --interval 轮询")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒数")
    parser.add_argument("--limit", type=int, default=20, help="单轮最多扫几个")
    args = parser.parse_args(argv)

    if not args.loop:
        try:
            run_once(args.limit)
        except Exception as exc:  # noqa: BLE001 - CLI 入口，失败要带上下文退出
            print(f"扫描失败：{exc}", file=sys.stderr)
            return 1
        return 0

    while True:
        try:
            run_once(args.limit)
        except Exception as exc:  # noqa: BLE001 - 单轮失败不退出，下一轮继续
            print(f"扫描失败：{exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
