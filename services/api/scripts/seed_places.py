"""CA-02 长沙种子数据入口。

用法（在 services/api 下执行）：

    python scripts/seed_places.py --check     # 只校验数据文件，不连库
    python scripts/seed_places.py --dry-run   # 连库试算，打印将要写入多少，随后回滚
    python scripts/seed_places.py             # 真正写入（可重复执行，第二次全是 0）

数据库地址取 app.core.config.settings.database_url，可用环境变量 DATABASE_URL 覆盖。
数据文件默认 app/modules/catalog/seed_data.json，可用 --data 指定。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 以 `python scripts/seed_places.py` 运行时，sys.path[0] 是 scripts/，需要把 services/api 加回来。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from app.modules.catalog.seed import apply_seed, load_seed_places  # noqa: E402

DEFAULT_DATA = ROOT / "app" / "modules" / "catalog" / "seed_data.json"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="灌入长沙文化点位种子数据")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="数据文件路径")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只校验数据文件，不连数据库")
    group.add_argument("--dry-run", action="store_true", help="连库试算后回滚，不入库")
    return parser.parse_args(argv)


def _describe(places) -> str:
    facts = sum(len(place.facts) for place in places)
    tags = sorted({tag for place in places for tag in place.culture_tags})
    districts = sorted({place.district for place in places})
    return (
        f"点位 {len(places)} 个，说法 {facts} 条，主题 {len(tags)} 类（{'、'.join(tags)}），"
        f"覆盖 {len(districts)} 个区县"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        places = load_seed_places(args.data)
    except ValidationError as exc:
        print(f"[失败] 数据校验不通过：{exc.error_count()} 处问题", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    except (OSError, KeyError, ValueError) as exc:
        print(f"[失败] 读不了数据文件 {args.data}：{exc}", file=sys.stderr)
        return 1

    print(f"[数据] {args.data}")
    print(f"[数据] {_describe(places)}")

    if args.check:
        print("[校验] 通过（未连数据库）")
        return 0

    # 只在真要写库时才 import：--check 不该因为连不上库而失败。
    from app.core.db import SessionLocal, engine  # noqa: PLC0415

    session = SessionLocal()
    try:
        report = apply_seed(session, places)
        if args.dry_run:
            session.rollback()
            print(f"[试算] {report.format()}（已回滚，未入库）")
        else:
            session.commit()
            print(f"[入库] {report.format()}")
    except Exception as exc:  # noqa: BLE001 - 脚本顶层要把任何失败都变成非零退出码
        session.rollback()
        print(f"[失败] 写库出错，已回滚：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
