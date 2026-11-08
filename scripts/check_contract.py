# -*- coding: utf-8 -*-
"""契约自检：语法、$ref 完整性、幂等头、分页约定。

CI 里跑；本地改完 openapi.yaml 也建议先跑一遍：
    python scripts/check_contract.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "api" / "openapi.yaml"

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def main() -> int:
    if not CONTRACT.exists():
        print(f"[FAIL] 找不到契约文件：{CONTRACT}")
        return 1

    raw = CONTRACT.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"[FAIL] YAML 解析失败：{exc}")
        return 1

    errors: list[str] = []
    warnings: list[str] = []

    # 1. $ref 是否都能解析
    refs = set(re.findall(r"\$ref:\s*['\"]?#/components/([A-Za-z]+)/([A-Za-z0-9_]+)", raw))
    components = doc.get("components", {})
    for group, name in sorted(refs):
        if name not in components.get(group, {}):
            errors.append(f"$ref 指向不存在的定义：{group}/{name}")

    # 2. 每个 operation 必须有 summary
    paths = doc.get("paths", {})
    op_count = 0
    for path, item in paths.items():
        for method, op in item.items():
            if method not in HTTP_METHODS:
                continue
            op_count += 1
            if not op.get("summary"):
                errors.append(f"{method.upper()} {path} 缺少 summary")

    # 3. 写操作（有请求体的 POST）必须声明 Idempotency-Key
    def has_idempotency(op: dict) -> bool:
        for param in op.get("parameters", []) or []:
            if isinstance(param, dict):
                ref = param.get("$ref", "")
                if "IdempotencyKey" in ref or param.get("name") == "Idempotency-Key":
                    return True
        return False

    exempt = {
        "/auth/register",
        "/auth/login",
        "/auth/refresh",
    }
    for path, item in paths.items():
        post = item.get("post")
        if not post or path in exempt:
            continue
        if not has_idempotency(post):
            warnings.append(f"POST {path} 未声明 Idempotency-Key")

    # 4. 列表端点必须用 cursor 分页，不能出现 offset
    for path, item in paths.items():
        get = item.get("get")
        if not get:
            continue
        names = {p.get("name") for p in (get.get("parameters") or []) if isinstance(p, dict)}
        if "offset" in names:
            errors.append(f"GET {path} 用了 offset 分页，应改为 cursor")

    # 5. 公开端点（security: []）不能误标需要鉴权，反之亦然
    unresolved = [p for p, i in paths.items() if not isinstance(i, dict)]
    for path in unresolved:
        errors.append(f"path 定义不是对象：{path}")

    # 6. B10：release 与投稿详情会返回 403/409（任务不属于我、状态不对），
    # 契约必须声明；这两个操作不在 routes 路由范围内，单独点名断言。
    for path, method, required in (
        ("/review/tasks/{task_id}/release", "post", {200, 403, 404, 409}),
        ("/submissions/{submission_id}", "get", {200, 403, 404}),
    ):
        op = (paths.get(path) or {}).get(method)
        if op is None:
            errors.append(f"{method.upper()} {path} 契约里找不到对应操作")
            continue
        declared = {
            int(code) for code in (op.get("responses") or {}) if str(code).isdigit()
        }
        for code in sorted(required - declared):
            errors.append(f"{method.upper()} {path} 契约缺状态码 {code}（B10）")

    print(f"契约文件：{CONTRACT.relative_to(ROOT)}")
    print(f"路径 {len(paths)} 个，操作 {op_count} 个，定义 {sum(len(v) for v in components.values())} 个")
    for w in warnings:
        print(f"[WARN] {w}")
    for e in errors:
        print(f"[FAIL] {e}")

    if errors:
        print(f"\n共 {len(errors)} 个错误，先修完再提交。")
        return 1
    print("\n契约自检通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
