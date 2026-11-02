"""ME-02：媒体扫描 worker。

用法（在 services/api 下）：
    python scripts/media_scan_worker.py --once          # 扫一轮就退出，适合 CI/本地验收
    python scripts/media_scan_worker.py --loop          # 常驻，每 --interval 秒扫一轮
    python scripts/media_scan_worker.py --once --limit 5

扫描做什么：解码 → 校验真实格式 → 重编码（清 EXIF/GPS）→ 生成缩略图 →
只有全部通过才把对象挪进 clean 前缀；任何一步失败都落 REJECTED，绝不转 CLEAN。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _storage():  # 延迟 import：--help 不依赖配置与依赖注入
    from app.adapters.storage import build_storage

    return build_storage()


def run_once(limit: int) -> int:
    from app.core.db import SessionLocal
    from app.modules.media.models import ScanState
    from app.modules.media.service import MediaService

    storage = _storage()
    with SessionLocal() as session:
        results = MediaService(session, storage).scan_pending(limit=limit)
        session.commit()
        clean = sum(1 for asset in results if asset.scan_state is ScanState.CLEAN)
        rejected = sum(1 for asset in results if asset.scan_state is ScanState.REJECTED)
    print(f"扫描 {len(results)} 个资产：clean={clean}, rejected={rejected}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="媒体扫描 worker（ME-02）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="只扫一轮就退出")
    mode.add_argument("--loop", action="store_true", help="常驻轮询")
    parser.add_argument("--interval", type=float, default=5.0, help="--loop 的轮询间隔（秒）")
    parser.add_argument("--limit", type=int, default=20, help="每轮最多处理多少个资产")
    args = parser.parse_args(argv)

    if args.once:
        return run_once(args.limit)

    while True:
        try:
            run_once(args.limit)
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能被单轮异常打死
            print(f"本轮扫描失败：{exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
