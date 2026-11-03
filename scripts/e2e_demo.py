"""演示链路自动验收：对着**已经跑起来**的后端（默认 127.0.0.1:8000）走一遍完整闭环。

覆盖：登录 → 发现页 → 提交规划 → agent 跑完 → 取点位 → 建草稿 → 写点位 → 投稿 →
审核员领取 → 通过 → 发布上线 → 新路线出现在发现页 → 收藏 + 评论。

前置：后端与 planner worker 都在跑，且库是 `scripts/seed_demo.py` 灌过的演示库。

用法（仓库根目录）：

    .\\services\\api\\.venv\\Scripts\\python.exe scripts/e2e_demo.py
    .\\services\\api\\.venv\\Scripts\\python.exe scripts/e2e_demo.py --base http://127.0.0.1:8000

全部通过时打印 `DEMO OK` 并以 0 退出；任一步失败时打印失败步骤与响应体，以 1 退出。
脚本会真的建一条路线并发布它（演示库里会多一条「演示用：…」的路线，介意就重置库）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

PASSWORD = "Demo12345!"
TITLE = "演示用：湘江边三站"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base.rstrip("/") + "/api/v1"

    def call(method: str, path: str, body=None, token=None, extra=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        if token:
            req.add_header("authorization", "Bearer " + token)
        for key, value in (extra or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw[:300].decode("utf-8", "replace")

    def idem() -> dict[str, str]:
        return {"Idempotency-Key": "demo-" + uuid.uuid4().hex[:12]}

    def login(username: str) -> str:
        status, data = call(
            "POST", "/auth/login", {"username": username, "password": PASSWORD}
        )
        assert status == 200, (status, data)
        return data["access_token"]

    def step(name: str, fn):
        try:
            result = fn()
        except Exception as exc:
            print(f"[FAIL] {name}: {exc}")
            raise SystemExit(1) from exc
        print(f"[OK] {name}" + (f" -> {result}" if result is not None else ""))
        return result

    user = login("demo@demo.local")
    print("[OK] 登录 demo@demo.local")
    reviewer = login("reviewer@demo.local")
    print("[OK] 登录 reviewer@demo.local")

    routes = step("发现页", lambda: call("GET", "/routes?limit=50")[1]["items"])
    step(
        "  已发布至少 2 条", lambda: len(routes) >= 2 or _fail(f"只有 {len(routes)} 条")
    )

    plan_id = step(
        "提交规划",
        lambda: call(
            "POST",
            "/plans",
            {"city": "changsha", "days": 1, "people": 2, "budget_total": 60000},
            token=user,
            extra=idem(),
        )[1]["id"],
    )

    def wait_plan() -> str:
        for _ in range(40):
            _, data = call("GET", f"/plans/{plan_id}", token=user)
            if data["state"] in ("READY", "FAILED"):
                if data["state"] == "FAILED":
                    _fail(data.get("error_code"))
                return f"READY，{len(data['days_plan'][0]['items'])} 个点位"
            time.sleep(3)
        _fail("40 秒没跑完：planner worker 没在跑？")

    step("agent 跑完规划", wait_plan)

    places = step("取点位", lambda: call("GET", "/places?limit=6")[1]["items"])
    place_ids = [p["id"] for p in places][:3]
    step(
        "  至少 3 个", lambda: len(place_ids) >= 3 or _fail(f"只有 {len(place_ids)} 个")
    )

    draft_id = step(
        "建草稿",
        lambda: call(
            "POST",
            "/route-drafts",
            {
                "title": TITLE,
                "summary": "演示验收自动生成",
                "theme_tags": ["历史"],
                "duration_minutes": 120,
            },
            token=user,
            extra=idem(),
        )[1]["id"],
    )

    def put_nodes() -> str:
        _, draft = call("GET", f"/route-drafts/{draft_id}", token=user)
        status, data = call(
            "PUT",
            f"/route-drafts/{draft_id}/nodes",
            {
                "nodes": [
                    {"place_id": pid, "stay_minutes": 30, "transport": "walk"}
                    for pid in place_ids
                ]
            },
            token=user,
            extra={"If-Match": str(draft.get("lock_version", 0)), **idem()},
        )
        assert status in (200, 204), (status, data)
        return "3 个点位写入"

    step("写点位", put_nodes)
    step(
        "投稿",
        lambda: call(
            "POST", f"/route-drafts/{draft_id}/submit", {}, token=user, extra=idem()
        ),
    )

    box: dict[str, str] = {}

    def claim() -> str:
        _, data = call("GET", "/review/tasks?limit=20", token=reviewer)
        opened = [t for t in data["items"] if t.get("state") == "OPEN"]
        if not opened:
            _fail("没有待审任务：投稿那步没成功？")
        task_id = opened[0]["id"]
        status, body = call(
            "POST", f"/review/tasks/{task_id}/claim", {}, token=reviewer, extra=idem()
        )
        assert status in (200, 201), (status, body)
        box["submission_id"] = opened[0]["submission_id"]
        return task_id

    task_id = step("审核员领取任务", claim)

    step(
        "审核通过",
        lambda: call(
            "POST",
            f"/review/tasks/{task_id}/decision",
            {"action": "approve", "reason": "演示验收：内容符合要求，来源可追溯"},
            token=reviewer,
            extra=idem(),
        ),
    )

    def publish() -> str:
        submission_id = box["submission_id"]
        status, sub = call("GET", f"/submissions/{submission_id}", token=reviewer)
        assert status == 200, (status, sub)
        body = {"version_id": sub["version_id"], "content_hash": sub["content_hash"]}
        status, data = call(
            "POST",
            f"/submissions/{submission_id}/publish",
            body,
            token=reviewer,
            extra=idem(),
        )
        assert status in (200, 201), (status, data)
        return data.get("state") or data.get("status")

    step("发布上线", publish)

    def visible() -> str:
        items = call("GET", "/routes?limit=50")[1]["items"]
        if not any(item["title"] == TITLE for item in items):
            _fail("新路线没出现在发现页")
        return f"公开 {len(items)} 条，新的那条在"

    step("出现在发现页", visible)

    def interact() -> str:
        items = call("GET", "/routes?limit=50")[1]["items"]
        route_id = next(item["id"] for item in items if item["title"] == TITLE)
        call("POST", f"/routes/{route_id}/favorite", None, token=user)
        status, data = call(
            "POST",
            f"/routes/{route_id}/comments",
            {"body": "演示评论：这条路线走下来很顺"},
            token=user,
            extra=idem(),
        )
        assert status in (200, 201), (status, data)
        return "收藏 + 评论成功"

    step("收藏 + 评论", interact)
    print("\nDEMO OK")
    return 0


def _fail(message):
    raise RuntimeError(message)


if __name__ == "__main__":
    sys.exit(main())
