"""RT-01：草稿的增删改查 + 权限 + 乐观锁 + 幂等。

覆盖卡面验收：匿名 401、本人可改、他人 403；另外把契约里的
If-Match（412）与 Idempotency-Key（409 复用冲突）一并钉住。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from app.modules.routes.models import DraftState, RouteDraft

API = "/api/v1/route-drafts"


def _key() -> str:
    return str(uuid.uuid4())


def _payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "title": "岳麓山到橘子洲半日",
        "summary": "从东方红广场上山，下山后坐地铁去橘子洲",
        "theme_tags": ["历史", "自然"],
        "duration_minutes": 240,
    }
    data.update(overrides)
    return data


def _create(client: TestClient, headers: dict[str, str], **overrides: object) -> dict:
    response = client.post(
        API,
        json=_payload(**overrides),
        headers={**headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _patch(
    client: TestClient,
    headers: dict[str, str],
    draft_id: str,
    *,
    if_match: int,
    key: str | None = None,
    **fields: object,
) -> Response:
    return client.patch(
        f"{API}/{draft_id}",
        json=fields,
        headers={
            **headers,
            "If-Match": str(if_match),
            "Idempotency-Key": key or _key(),
        },
    )


# ===== 认证 =====
def test_list_requires_login(client: TestClient) -> None:
    response = client.get(API)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_create_requires_login(client: TestClient) -> None:
    response = client.post(API, json=_payload(), headers={"Idempotency-Key": _key()})
    assert response.status_code == 401


def test_create_without_idempotency_key_is_422(client: TestClient, user_headers: dict) -> None:
    response = client.post(API, json=_payload(), headers=user_headers)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "Idempotency-Key"


# ===== 新建 =====
def test_create_returns_contract_shape(client: TestClient, user_headers: dict) -> None:
    body = _create(client, user_headers)
    assert set(body) == {
        "id",
        "route_id",
        "title",
        "summary",
        "state",
        "lock_version",
        "base_version_id",
        "updated_at",
    }
    assert body["route_id"] is None
    assert body["state"] == "EDITING"
    assert body["lock_version"] == 1
    assert body["title"] == "岳麓山到橘子洲半日"
    assert body["updated_at"], "updated_at 必须由数据库回填出来"


def test_create_same_key_replays_same_draft(client: TestClient, user_headers: dict) -> None:
    key = _key()
    headers = {**user_headers, "Idempotency-Key": key}
    first = client.post(API, json=_payload(), headers=headers)
    second = client.post(API, json=_payload(), headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_create_same_key_different_body_is_conflict(client: TestClient, user_headers: dict) -> None:
    headers = {**user_headers, "Idempotency-Key": _key()}
    client.post(API, json=_payload(), headers=headers)
    response = client.post(API, json=_payload(title="另一条路线"), headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_create_title_too_long_is_422(client: TestClient, user_headers: dict) -> None:
    response = client.post(
        API,
        json=_payload(title="长" * 61),
        headers={**user_headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 422


# ===== 列表 =====
def test_list_only_returns_own_drafts(
    client: TestClient, user_headers: dict, admin_headers: dict
) -> None:
    mine = _create(client, user_headers)
    _create(client, admin_headers)

    body = client.get(API, headers=user_headers).json()
    assert [item["id"] for item in body["items"]] == [mine["id"]]
    assert body["next_cursor"] is None


def test_list_paginates_with_cursor(client: TestClient, user_headers: dict) -> None:
    created = {_create(client, user_headers)["id"] for _ in range(3)}

    first = client.get(API, params={"limit": 2}, headers=user_headers).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = client.get(
        API,
        params={"limit": 2, "cursor": first["next_cursor"]},
        headers=user_headers,
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None

    seen = [item["id"] for item in first["items"] + second["items"]]
    assert set(seen) == created, "翻页不能漏也不能重复"
    assert len(seen) == len(set(seen))


def test_list_bad_cursor_is_400(client: TestClient, user_headers: dict) -> None:
    response = client.get(API, params={"cursor": "not-a-cursor"}, headers=user_headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


# ===== 详情 =====
def test_detail_own_draft_has_empty_nodes(client: TestClient, user_headers: dict) -> None:
    draft = _create(client, user_headers)
    response = client.get(f"{API}/{draft['id']}", headers=user_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == draft["id"]
    # 节点表在 RT-02 才建：现在必须是空数组，而不是 404 或省略字段。
    assert body["nodes"] == []
    assert body["cost"] == {
        "min_total": 0,
        "max_total": 0,
        "currency": "CNY",
        "unknown_count": 0,
    }


def test_detail_other_user_is_forbidden(
    client: TestClient, user_headers: dict, admin_headers: dict
) -> None:
    draft = _create(client, user_headers)
    response = client.get(f"{API}/{draft['id']}", headers=admin_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "DRAFT_FORBIDDEN"


def test_detail_missing_is_404(client: TestClient, user_headers: dict) -> None:
    response = client.get(f"{API}/{uuid.uuid4()}", headers=user_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DRAFT_NOT_FOUND"


# ===== 更新 =====
def test_patch_without_if_match_is_422(client: TestClient, user_headers: dict) -> None:
    draft = _create(client, user_headers)
    response = client.patch(
        f"{API}/{draft['id']}",
        json={"title": "新标题"},
        headers={**user_headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "If-Match"


def test_patch_stale_version_is_412(client: TestClient, user_headers: dict) -> None:
    draft = _create(client, user_headers)
    response = _patch(client, user_headers, draft["id"], if_match=0, title="新标题")
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "STALE_DRAFT_VERSION"


def test_patch_updates_and_bumps_lock_version(client: TestClient, user_headers: dict) -> None:
    draft = _create(client, user_headers)
    response = _patch(
        client,
        user_headers,
        draft["id"],
        if_match=1,
        title="改过的标题",
        summary="改过的简介",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "改过的标题"
    assert body["summary"] == "改过的简介"
    assert body["lock_version"] == 2
    # 契约的 RouteDraft 不返回 theme_tags，响应里就不该冒出来。
    assert "theme_tags" not in body

    # 只带 title 的 PATCH 不能顺带清空 summary/theme_tags。
    again = _patch(client, user_headers, draft["id"], if_match=2, theme_tags=["非遗"])
    assert again.status_code == 200
    refreshed = client.get(f"{API}/{draft['id']}", headers=user_headers).json()
    assert refreshed["summary"] == "改过的简介"
    assert refreshed["lock_version"] == 3


def test_patch_other_user_is_forbidden(
    client: TestClient, user_headers: dict, admin_headers: dict
) -> None:
    draft = _create(client, user_headers)
    response = _patch(client, admin_headers, draft["id"], if_match=1, title="抢改")
    assert response.status_code == 403


def test_patch_locked_draft_is_409(
    client: TestClient, user_headers: dict, db_session: Session
) -> None:
    draft = _create(client, user_headers)
    row = db_session.get(RouteDraft, uuid.UUID(draft["id"]))
    assert row is not None
    row.state = DraftState.SUBMITTED_LOCKED
    db_session.commit()

    response = _patch(client, user_headers, draft["id"], if_match=1, title="锁定后改")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_NOT_EDITABLE"


def test_patch_same_key_replays_without_bumping_version(
    client: TestClient, user_headers: dict
) -> None:
    draft = _create(client, user_headers)
    key = _key()
    first = _patch(client, user_headers, draft["id"], if_match=1, key=key, title="一次")
    second = _patch(client, user_headers, draft["id"], if_match=2, key=key, title="一次")
    assert first.status_code == second.status_code == 200
    assert second.json()["lock_version"] == 2, "同一请求重试不能再自增版本"


def test_patch_same_key_different_body_is_conflict(client: TestClient, user_headers: dict) -> None:
    draft = _create(client, user_headers)
    key = _key()
    _patch(client, user_headers, draft["id"], if_match=1, key=key, title="一次")
    response = _patch(client, user_headers, draft["id"], if_match=2, key=key, title="两次")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ===== 删除 =====
def test_delete_editing_draft(client: TestClient, user_headers: dict) -> None:
    draft = _create(client, user_headers)
    response = client.delete(f"{API}/{draft['id']}", headers=user_headers)
    assert response.status_code == 204
    assert client.get(f"{API}/{draft['id']}", headers=user_headers).status_code == 404


def test_delete_locked_draft_is_409(
    client: TestClient, user_headers: dict, db_session: Session
) -> None:
    draft = _create(client, user_headers)
    row = db_session.get(RouteDraft, uuid.UUID(draft["id"]))
    assert row is not None
    row.state = DraftState.APPROVED_LOCKED
    db_session.commit()

    response = client.delete(f"{API}/{draft['id']}", headers=user_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_NOT_DELETABLE"
