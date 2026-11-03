"""QA-01：权限负例矩阵的接口级用例。

矩阵本体在 `tests/authz_matrix.yaml`（接口 + 方法 + 调用者角色 + 期望状态码）。
这里做三件事：

1. 把 yaml 每条变成一个真实 TestClient 请求：按 `scenario` 现场造出它需要的资源，
   用对应角色签发的 access token 去打，而不是靠手搓 status 断言；
2. 断言状态码（以及可选的 `error.code`）；
3. 整文件跑完后校验「yaml 条数 == 实际执行条数」，漏写一条没人发现是不可能的。

最后一条 `test_every_matrix_case_was_executed` 依赖执行顺序（pytest 按定义顺序跑），
用 `-k` 只跑部分用例时它会 skip：全量校验只在整文件运行时成立。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.deps import create_access_token

API = "/api/v1"
MATRIX_PATH = Path(__file__).with_name("authz_matrix.yaml")

# 令牌里的身份。user/admin 的 id 与根 conftest 种进内存库的账号一致；
# peer 是第二个普通用户，只用来验证跨用户越权，不需要有账号行。
_ACTORS = {
    "user": ("22222222-2222-2222-2222-222222222222", "xiaoming", "user"),
    "peer": ("55555555-5555-5555-5555-555555555555", "lisi", "user"),
    "editor": ("33333333-3333-3333-3333-333333333333", "nina", "editor"),
    "admin": ("11111111-1111-1111-1111-111111111111", "boss", "admin"),
}
_ANONYMOUS = "anonymous"

_REQUIRED_FIELDS = {"id", "method", "path", "actor", "scenario", "expected", "note"}


def _headers_for(actor: str) -> dict[str, str]:
    if actor == _ANONYMOUS:
        return {}
    user_id, username, role = _ACTORS[actor]
    token = create_access_token(user_id=user_id, username=username, role=role)
    return {"Authorization": f"Bearer {token}"}


def _request(
    client: TestClient,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    json: Any | None = None,
) -> Any:
    """统一发请求：非 GET 自动补一个新鲜的幂等键，别让 422 抢在鉴权前面。"""
    merged = dict(headers or {})
    if method != "GET":
        merged.setdefault("Idempotency-Key", str(uuid.uuid4()))
    return client.request(method, path, headers=merged, json=json)


# ===== 造数原语（全部走真实接口，不直接写库） =====


def _create_draft(client: TestClient, headers: dict[str, str], *, title: str = "矩阵草稿") -> str:
    response = _request(client, "POST", f"{API}/route-drafts", headers, json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _submit(client: TestClient, headers: dict[str, str], draft_id: str) -> dict[str, Any]:
    response = _request(
        client,
        "POST",
        f"{API}/route-drafts/{draft_id}/submit",
        headers,
        json={"note": "矩阵用例送审"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_plan(client: TestClient, headers: dict[str, str]) -> str:
    response = _request(
        client,
        "POST",
        f"{API}/plans",
        headers,
        json={"city": "长沙", "days": 2, "people": 2, "budget_total": 50000},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _create_place(client: TestClient, headers: dict[str, str]) -> str:
    response = _request(
        client,
        "POST",
        f"{API}/places",
        headers,
        json={"name": "矩阵地点", "district": "岳麓区", "lat": 28.1871, "lon": 112.9514},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _task_for(client: TestClient, headers: dict[str, str], submission_id: str) -> dict[str, Any]:
    response = client.get(f"{API}/review/tasks", headers=headers, params={"limit": 50})
    assert response.status_code == 200, response.text
    for task in response.json()["items"]:
        if task["submission_id"] == submission_id:
            return dict(task)
    raise AssertionError(f"review task not found for {submission_id}")


def _claim(client: TestClient, headers: dict[str, str], task_id: str) -> None:
    response = _request(client, "POST", f"{API}/review/tasks/{task_id}/claim", headers)
    assert response.status_code == 200, response.text


def _approve(client: TestClient, headers: dict[str, str], submission_id: str) -> None:
    task = _task_for(client, headers, submission_id)
    _claim(client, headers, task["id"])
    response = _request(
        client,
        "POST",
        f"{API}/review/tasks/{task['id']}/decision",
        headers,
        json={"action": "approve"},
    )
    assert response.status_code == 200, response.text


def _publish(client: TestClient, headers: dict[str, str], submission_id: str) -> dict[str, Any]:
    detail = client.get(f"{API}/submissions/{submission_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    response = _request(
        client,
        "POST",
        f"{API}/submissions/{submission_id}/publish",
        headers,
        json={"version_id": body["version_id"], "content_hash": body["content_hash"]},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ===== 每个 scenario 负责「这条请求前面的真实业务链」 =====


def _scenario_none(client: TestClient) -> dict[str, Any]:
    return {}


def _scenario_draft_owned_by_user(client: TestClient) -> dict[str, Any]:
    draft_id = _create_draft(client, _headers_for("user"))
    return {"params": {"draft_id": draft_id}}


def _scenario_plan_owned_by_user(client: TestClient) -> dict[str, Any]:
    plan_id = _create_plan(client, _headers_for("user"))
    return {"params": {"plan_id": plan_id}}


def _scenario_place_exists(client: TestClient) -> dict[str, Any]:
    place_id = _create_place(client, _headers_for("admin"))
    return {"params": {"place_id": place_id}}


def _scenario_open_task_owned_by_user(client: TestClient) -> dict[str, Any]:
    """作者是 user，任务还开着（没被任何人领取）。"""
    submission = _submit(client, _headers_for("user"), _create_draft(client, _headers_for("user")))
    task = _task_for(client, _headers_for("editor"), submission["id"])
    return {"params": {"task_id": task["id"], "submission_id": submission["id"]}}


def _scenario_claimed_own_task(client: TestClient) -> dict[str, Any]:
    """editor 投自己的稿，再自己领走任务——下一步的决定应被利益冲突拦下。"""
    editor = _headers_for("editor")
    draft_id = _create_draft(client, editor, title="审核员自己的草稿")
    submission = _submit(client, editor, draft_id)
    task = _task_for(client, editor, submission["id"])
    _claim(client, editor, task["id"])
    return {"params": {"task_id": task["id"], "submission_id": submission["id"]}}


def _scenario_claimed_peer_task_by_admin(client: TestClient) -> dict[str, Any]:
    """user 投稿，admin 领取——admin 不是作者，审核应通过。"""
    submission = _submit(client, _headers_for("user"), _create_draft(client, _headers_for("user")))
    admin = _headers_for("admin")
    task = _task_for(client, admin, submission["id"])
    _claim(client, admin, task["id"])
    return {"params": {"task_id": task["id"], "submission_id": submission["id"]}}


def _scenario_approved_submission_owned_by_user(client: TestClient) -> dict[str, Any]:
    """user 投稿、editor 审核通过；顺带把发布请求体（version_id/hash）带出来。"""
    submission = _submit(client, _headers_for("user"), _create_draft(client, _headers_for("user")))
    editor = _headers_for("editor")
    _approve(client, editor, submission["id"])
    detail = client.get(f"{API}/submissions/{submission['id']}", headers=editor)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    return {
        "params": {"submission_id": submission["id"]},
        "json": {"version_id": body["version_id"], "content_hash": body["content_hash"]},
    }


def _scenario_user_submission_pending(client: TestClient) -> dict[str, Any]:
    submission = _submit(client, _headers_for("user"), _create_draft(client, _headers_for("user")))
    return {"params": {"submission_id": submission["id"]}}


def _scenario_publication_owned_by_user(client: TestClient) -> dict[str, Any]:
    """投稿（user）→ 通过（editor）→ 发布（editor），拿到真实 publication id。"""
    submission = _submit(client, _headers_for("user"), _create_draft(client, _headers_for("user")))
    editor = _headers_for("editor")
    _approve(client, editor, submission["id"])
    publication = _publish(client, editor, submission["id"])
    return {"params": {"publication_id": publication["id"]}}


SCENARIOS: dict[str, Callable[[TestClient], dict[str, Any]]] = {
    "none": _scenario_none,
    "draft_owned_by_user": _scenario_draft_owned_by_user,
    "plan_owned_by_user": _scenario_plan_owned_by_user,
    "place_exists": _scenario_place_exists,
    "open_task_owned_by_user": _scenario_open_task_owned_by_user,
    "claimed_own_task": _scenario_claimed_own_task,
    "claimed_peer_task_by_admin": _scenario_claimed_peer_task_by_admin,
    "approved_submission_owned_by_user": _scenario_approved_submission_owned_by_user,
    "user_submission_pending": _scenario_user_submission_pending,
    "publication_owned_by_user": _scenario_publication_owned_by_user,
}


# ===== 矩阵读取与逐条执行 =====

_MATRIX_DATA: dict[str, Any] = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
MATRIX: list[dict[str, Any]] = _MATRIX_DATA["cases"]

# 每条执行过的用例 id：最后一条测试拿它和 yaml 对账，证明没有条目被漏掉。
_EXECUTED: set[str] = set()


@pytest.mark.parametrize("case", MATRIX, ids=[case["id"] for case in MATRIX])
def test_authz_case(case: dict[str, Any], client: TestClient) -> None:
    prepared = SCENARIOS[case["scenario"]](client)
    path = case["path"].format(**prepared.get("params", {}))
    body = prepared.get("json", case.get("json"))
    headers = {**_headers_for(case["actor"]), **(case.get("headers") or {})}

    response = _request(client, case["method"], path, headers, json=body)

    assert response.status_code == case["expected"], (
        f"{case['id']}: {case['method']} {path} 期望 {case['expected']}，"
        f"实际 {response.status_code}，body={response.text}"
    )
    expect_code = case.get("expect_code")
    if expect_code:
        assert response.json()["error"]["code"] == expect_code
    _EXECUTED.add(case["id"])


def test_matrix_is_well_formed() -> None:
    """yaml 本身的结构问题要报在夹具之外，别混进逐条用例里。"""
    ids = [case["id"] for case in MATRIX]
    assert len(ids) == len(set(ids)), f"用例 id 重复：{sorted(ids)}"

    for case in MATRIX:
        missing = _REQUIRED_FIELDS - case.keys()
        assert not missing, f"{case['id']} 缺字段：{sorted(missing)}"
        assert case["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}, case["id"]
        assert case["actor"] in {_ANONYMOUS, *_ACTORS}, case["id"]
        assert case["scenario"] in SCENARIOS, case["id"]
        assert isinstance(case["expected"], int), case["id"]
        if case["expected"] >= 400:
            assert case.get("expect_code"), f"{case['id']} 是负例，应写 expect_code"


def test_every_matrix_case_was_executed(request: pytest.FixtureRequest) -> None:
    """yaml 条数 == 实际执行条数；漏写一条这里就红。

    必须放在文件最后：pytest 按定义顺序执行，它天然跑在全部逐条用例之后。
    """
    if request.config.getoption("keyword", default=""):
        pytest.skip("用 -k 选择性运行时不做全量对账")
    expected_ids = {entry["id"] for entry in MATRIX}
    not_executed = expected_ids - _EXECUTED
    assert not not_executed, f"矩阵里有条目没被执行：{sorted(not_executed)}"
    assert expected_ids >= _EXECUTED, f"执行了不在矩阵里的用例：{sorted(_EXECUTED - expected_ids)}"
