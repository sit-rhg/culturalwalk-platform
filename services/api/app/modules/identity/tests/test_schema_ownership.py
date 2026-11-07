"""DTO 归属守护：同一个契约对象别在多个模块各长一份。

背景：#105 之后 identity 曾同时存在三份 route 卡片 DTO——自己一份、community 一份、
routes 里还有 CostSummaryOut；三份只要有一处改字段，另外两处的响应就会静默漂移，
而且是"看起来都对"的那种错。这里把归属钉住：完整 RouteSummary 归 community，
identity 只保留公开主页真正会填的子集。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
MODULES_ROOT = APP_ROOT / "modules"


def _classes_in(path: Path) -> set[str]:
    # 数 AST 而不是 import：这条用例不该因为某个模块 import 失败而连带红掉。
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _definitions_of(class_name: str) -> set[str]:
    owners: set[str] = set()
    for schema_file in MODULES_ROOT.glob("*/schemas.py"):
        if class_name in _classes_in(schema_file):
            owners.add(schema_file.parent.name)
    return owners


def test_route_summary_is_defined_in_one_place_only() -> None:
    assert _definitions_of("RouteSummaryOut") == {"community"}


def test_identity_keeps_only_its_own_profile_subset() -> None:
    identity_classes = _classes_in(MODULES_ROOT / "identity" / "schemas.py")
    # 主页用的是 ProfileRouteOut（刻意去掉 community 才数得准的聚合字段）。
    assert "ProfileRouteOut" in identity_classes
    assert "RouteSummaryOut" not in identity_classes
    assert "RouteAuthorOut" not in identity_classes
    assert "CostSummaryOut" not in identity_classes
