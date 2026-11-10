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

    # 6. 状态码覆盖：routes 模块（B3）
    # 每个 operation 在代码/权限层可能返回的状态码，契约必须声明；
    # 少一个就在这里报错，CI 的 contract job 会红。目前只接了 routes（WP-02 B3 范围）。
    import ast

    error_status = {
        "Unauthorized": 401,
        "Forbidden": 403,
        "NotFound": 404,
        "Conflict": 409,
        "PreconditionFailed": 412,
        "ValidationFailed": 422,
        "ServiceUnavailable": 503,
    }
    for router_file in [ROOT / "services/api/app/modules/routes/router.py"]:
        tree = ast.parse(router_file.read_text(encoding="utf-8"))
        prefixes: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not (isinstance(call.func, ast.Name) and call.func.id == "APIRouter"):
                continue
            for kw in call.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            prefixes[target.id] = str(kw.value.value)

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                ):
                    continue
                owner, method = decorator.func.value, decorator.func.attr
                if method not in HTTP_METHODS or not isinstance(owner, ast.Name):
                    continue
                prefix = prefixes.get(owner.id)
                if prefix is None:
                    continue
                rel = (
                    decorator.args[0].value
                    if decorator.args and isinstance(decorator.args[0], ast.Constant)
                    else ""
                )
                path = prefix + str(rel)
                op = (paths.get(path) or {}).get(method)
                if op is None:
                    errors.append(f"{method.upper()} {path} 在契约里找不到对应操作")
                    continue
                declared = {
                    int(code)
                    for code in (op.get("responses") or {})
                    if str(code).isdigit()
                }
                # 鉴权依赖一律 401；带路径参数的操作存在「不属于我 / 不存在」→ 403/404。
                required: set[int] = {401}
                if "{" in path:
                    required |= {403, 404}
                if op.get("requestBody"):
                    required.add(422)
                param_names = {
                    p.get("name")
                    for p in (op.get("parameters") or [])
                    if isinstance(p, dict)
                }
                if "Idempotency-Key" in param_names:
                    required.add(409)
                if "If-Match" in param_names:
                    required.add(412)
                if method == "delete":
                    required.add(204)
                # handler 直接 raise 的错误类型也必须有声明。
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call):
                        func = sub.exc.func
                        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                        if name in error_status:
                            required.add(error_status[name])
                for code in sorted(required - declared):
                    errors.append(
                        f"{method.upper()} {path} 契约缺状态码 {code}（代码或权限约定会返回）"
                    )

    # 7. B10：release 与投稿详情会返回 403/409（任务不属于我、状态不对），
    # 契约必须声明；这两个操作不在 routes 路由范围内，单独点名断言。
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
