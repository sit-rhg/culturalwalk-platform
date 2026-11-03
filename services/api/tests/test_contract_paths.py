"""契约（docs/api/openapi.yaml）和真正挂载的路由必须对得上。

这条用例是有来历的：#105 的个人主页端点被一次「补齐 26 个契约接口」的提交整段覆盖
router.py 时静默丢掉——契约里有、服务端没有，CI 四条全绿，没有任何一条用例发现。
所以这里把「契约 → 服务端」和「服务端 → 契约」两个方向都钉住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "docs" / "api" / "openapi.yaml"
API_PREFIX = "/api/v1"
METHODS = ("get", "post", "put", "patch", "delete")

# 契约里有、服务端确实还没挂的。这是欠账清单，不是白名单：
# 谁把它实现了，test_known_gaps_are_still_missing 就会要求从这里删掉那一行。
KNOWN_GAPS: dict[tuple[str, str], str] = {
    ("/plans", "get"): "行程列表：planner 只实现了提交与详情，列表还是契约先行",
    ("/plans/{plan_id}", "delete"): "删除行程：同上",
}


def _contract_ops() -> dict[str, set[str]]:
    spec: dict[str, Any] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return {path: {m for m in ops if m in METHODS} for path, ops in spec["paths"].items()}


def _mounted_ops() -> dict[str, set[str]]:
    spec: dict[str, Any] = create_app().openapi()
    return {
        path.removeprefix(API_PREFIX): {m for m in ops if m in METHODS}
        for path, ops in spec["paths"].items()
    }


def _missing_from_app() -> set[tuple[str, str]]:
    contract, mounted = _contract_ops(), _mounted_ops()
    return {
        (path, method)
        for path, methods in contract.items()
        for method in methods
        if method not in mounted.get(path, set())
    }


def test_contract_operations_are_mounted() -> None:
    unexpected = _missing_from_app() - set(KNOWN_GAPS)
    assert not unexpected, "契约里有、服务端没挂（且不在已知欠账里）：" + "、".join(
        f"{m.upper()} {p}" for p, m in sorted(unexpected)
    )


def test_known_gaps_are_still_missing() -> None:
    still_missing = _missing_from_app()
    filled = [gap for gap in KNOWN_GAPS if gap not in still_missing]
    assert not filled, "这些接口已经实现，请从 KNOWN_GAPS 里删掉：" + "、".join(
        f"{m.upper()} {p}" for p, m in sorted(filled)
    )


def test_no_endpoint_without_contract() -> None:
    contract, mounted = _contract_ops(), _mounted_ops()
    extra = {
        (path, method)
        for path, methods in mounted.items()
        for method in methods
        if method not in contract.get(path, set())
    }
    assert not extra, "服务端挂了契约里没有的接口（不许自己发明端点）：" + "、".join(
        f"{m.upper()} {p}" for p, m in sorted(extra)
    )
