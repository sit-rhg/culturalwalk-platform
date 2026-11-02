"""端到端冒烟：注册 → 登录 → /users/me → 查地点 → 建草稿 → 写节点 → 读详情 → 删除。

前置条件：同一个库已执行 `alembic upgrade head`，且跑过 `scripts/seed_demo.py`
（地点列表至少要有 2 条可用的已发布点位，否则第 5 步会给出明确提示）。

用法（在 services/api 目录下执行）：

    .venv/Scripts/python.exe ../../scripts/e2e_smoke.py --database-url "sqlite:///./demo.db"

PowerShell：

    .venv/Scripts/python.exe ../../scripts/e2e_smoke.py --database-url "sqlite:///./demo.db"

全部通过时打印 `E2E OK` 并以 0 退出；任一步失败时打印失败步骤、HTTP 状态与响应体，
并以 1 退出。脚本会删掉自己创建的草稿，但注册的冒烟用户会留在库里（用户名带随机后缀）。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "services" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

PASSWORD = "Demo12345!"
API = "/api/v1"


class StepFailed(RuntimeError):
    """某一步的断言失败；由 main 转成非零退出码与可读输出。"""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _database_url_from_argv(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--database-url" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--database-url="):
            return arg.split("=", 1)[1]
    return None


_url = _database_url_from_argv(sys.argv[1:])
if _url:
    os.environ["DATABASE_URL"] = _url

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CulturalWalk 端到端冒烟")
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖 DATABASE_URL，必须与灌种子时用的库一致",
    )
    return parser.parse_args(argv)


def _expect(resp, status: int, step: str, what: str = "") -> None:
    if resp.status_code != status:
        raise StepFailed(
            step,
            f"期望 HTTP {status}，实际 {resp.status_code}；{what}响应：{resp.text[:600]}",
        )


def _json(resp, step: str) -> dict:
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise StepFailed(step, f"响应不是 JSON：{resp.text[:400]}") from exc
    if not isinstance(body, dict):
        raise StepFailed(step, f"期望 JSON 对象，实际：{body!r}")
    return body


def _check(condition: object, step: str, message: str) -> None:
    if not condition:
        raise StepFailed(step, message)


def run(client: TestClient) -> None:
    draft_id: str | None = None
    token: str | None = None
    deleted = False
    try:
        # 1. 健康检查
        step = "1/9 健康检查"
        resp = client.get(f"{API}/health")
        _expect(resp, 200, step)
        _check(
            _json(resp, step).get("status") == "ok", step, f"健康检查返回异常：{resp.text[:300]}"
        )
        print(f"[{step}] 200 OK")

        # 2. 注册（用户名带随机后缀，避免与上一次冒烟冲突）
        step = "2/9 注册新用户"
        suffix = uuid.uuid4().hex[:10]
        username = f"e2e_{suffix}"
        email = f"{username}@example.com"
        resp = client.post(
            f"{API}/auth/register",
            json={"username": username, "email": email, "password": PASSWORD},
        )
        _expect(resp, 201, step)
        registered = _json(resp, step)
        _check(bool(registered.get("access_token")), step, "注册响应缺少 access_token")
        _check(bool(registered.get("refresh_token")), step, "注册响应缺少 refresh_token")
        user = registered.get("user") or {}
        _check(
            user.get("username") == username, step, f"注册返回的用户名不对：{user.get('username')}"
        )
        _check(
            user.get("role") == "user", step, f"新用户默认角色应为 user，实际：{user.get('role')}"
        )
        user_id = user.get("id")
        _check(bool(user_id), step, "注册响应缺少 user.id")
        print(f"[{step}] 201 {username}")

        # 3. 登录
        step = "3/9 登录"
        resp = client.post(
            f"{API}/auth/login",
            json={"username": username, "password": PASSWORD},
        )
        _expect(resp, 200, step)
        tokens = _json(resp, step)
        token = tokens.get("access_token")  # noqa: F841 - 失败清理时要用
        _check(bool(token), step, "登录响应缺少 access_token")
        _check((tokens.get("user") or {}).get("id") == user_id, step, "登录返回的 user.id 不一致")
        print(f"[{step}] 200 token 已签发")

        headers = {"Authorization": f"Bearer {token}"}

        # 4. 当前用户
        step = "4/9 GET /users/me"
        resp = client.get(f"{API}/users/me", headers=headers)
        _expect(resp, 200, step)
        me = _json(resp, step)
        _check(me.get("id") == user_id, step, f"/users/me 的 id 不一致：{me.get('id')}")
        _check(me.get("username") == username, step, "/users/me 的用户名不一致")
        print(f"[{step}] 200 {me.get('username')} / {me.get('role')}")

        # 5. 地点列表（种子数据的前置检查）
        step = "5/9 GET /places"
        resp = client.get(f"{API}/places", params={"limit": 10})
        _expect(resp, 200, step)
        places_body = _json(resp, step)
        items = places_body.get("items")
        _check(isinstance(items, list), step, "响应缺少 items 数组")
        if len(items) < 2:
            raise StepFailed(
                step,
                f"只有 {len(items)} 个地点，无法继续；请先运行 scripts/seed_demo.py",
            )
        for place in items:
            _check(
                bool(place.get("id")) and bool(place.get("name")), step, f"地点字段不完整：{place}"
            )
            _check(
                place.get("lat") is not None and place.get("lon") is not None, step, "地点缺经纬度"
            )
            _check(
                place.get("status") == "verified",
                step,
                f"公开列表里出现了非 verified 点位：{place}",
            )
            _check(bool(place.get("summary")), step, "地点缺少文化说明 summary")
        first, second = items[0], items[1]
        print(f"[{step}] 200 {len(items)} 条，首个：{first.get('name')}")

        # 6. 创建草稿
        step = "6/9 POST /route-drafts"
        resp = client.post(
            f"{API}/route-drafts",
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            json={
                "title": f"E2E 冒烟路线 {suffix[:4]}",
                "summary": "端到端冒烟创建的临时草稿，跑完会删除。",
                "theme_tags": ["历史", "建筑"],
                "duration_minutes": 90,
            },
        )
        _expect(resp, 201, step)
        draft = _json(resp, step)
        _check(
            draft.get("state") == "EDITING",
            step,
            f"新草稿状态应为 EDITING，实际：{draft.get('state')}",
        )
        _check(draft.get("lock_version") == 1, step, "新草稿 lock_version 应为 1")
        draft_id = draft.get("id")
        _check(bool(draft_id), step, "创建响应缺少草稿 id")
        print(f"[{step}] 201 draft={draft_id}")

        # 7. 写节点（整表替换 + If-Match 乐观锁）
        step = "7/9 PUT /route-drafts/{id}/nodes"
        resp = client.put(
            f"{API}/route-drafts/{draft_id}/nodes",
            headers={**headers, "If-Match": "1", "Idempotency-Key": uuid.uuid4().hex},
            json={
                "nodes": [
                    {
                        "place_id": first["id"],
                        "stay_minutes": 45,
                        "transport": "walk",
                        "note": "冒烟脚本写入的节点 1",
                    },
                    {
                        "place_id": second["id"],
                        "stay_minutes": 60,
                        "transport": "metro",
                        "note": "冒烟脚本写入的节点 2",
                    },
                ]
            },
        )
        _expect(resp, 200, step)
        detail = _json(resp, step)
        nodes = detail.get("nodes") or []
        _check(len(nodes) == 2, step, f"期望 2 个节点，实际：{len(nodes)}")
        _check([node.get("position") for node in nodes] == [1, 2], step, "节点顺序应为 1,2")
        _check(
            [node.get("place", {}).get("id") for node in nodes] == [first["id"], second["id"]],
            step,
            "节点地点顺序不一致",
        )
        _check(
            [node.get("stay_minutes") for node in nodes] == [45, 60], step, "停留时长未按请求落库"
        )
        _check(
            [node.get("transport") for node in nodes] == ["walk", "metro"],
            step,
            "交通方式未按请求落库",
        )
        _check(
            detail.get("lock_version") == 2,
            step,
            f"写节点后 lock_version 应为 2，实际：{detail.get('lock_version')}",
        )
        print(f"[{step}] 200 nodes=2 lock_version={detail.get('lock_version')}")

        # 8. 读详情确认持久化
        step = "8/9 GET /route-drafts/{id}"
        resp = client.get(f"{API}/route-drafts/{draft_id}", headers=headers)
        _expect(resp, 200, step)
        persisted = _json(resp, step)
        _check(len(persisted.get("nodes") or []) == 2, step, "详情里没有读回 2 个节点")
        _check(
            [node.get("place", {}).get("name") for node in persisted["nodes"]]
            == [first["name"], second["name"]],
            step,
            "详情里的地点名与列表不一致",
        )
        print(f"[{step}] 200 读回 2 个节点")

        # 9. 删除临时草稿（EDITING 状态允许删除）
        step = "9/9 DELETE /route-drafts/{id}"
        resp = client.delete(f"{API}/route-drafts/{draft_id}", headers=headers)
        _expect(resp, 204, step)
        deleted = True
        resp = client.get(f"{API}/route-drafts/{draft_id}", headers=headers)
        _expect(resp, 404, step, "删除后再读应该 404；")
        print(f"[{step}] 204，再读 404")

    finally:
        # 中途失败也尽量把临时草稿删掉，不给演示库留垃圾。
        if draft_id and not deleted and token:
            # 清理失败不应盖住真正的失败原因。
            with contextlib.suppress(Exception):
                client.delete(
                    f"{API}/route-drafts/{draft_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _parse_args(argv)
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    print(f"[环境] 数据库：{make_url(settings.database_url).render_as_string(hide_password=True)}")
    try:
        with TestClient(app) as client:
            run(client)
    except StepFailed as exc:
        print(f"[FAIL] {exc.step}：{exc.detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 未预期异常也要给出非零退出码
        print(f"[FAIL] 未预期异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print("E2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
