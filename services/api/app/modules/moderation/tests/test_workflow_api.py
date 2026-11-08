"""审核发布闭环的接口级测试：正常流 + 越权 / 非法状态 / hash 不匹配 / 幂等负例。

全部走 TestClient 与 conftest 的内存 SQLite，不连真库。审核员用 editor 角色
（仓库没有独立 reviewer 角色，见 docs/00 §5 与 identity 模块的角色定义）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import create_access_token
from app.main import app
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditService
from app.modules.moderation.models import Publication, PublicationState, Submission
from app.modules.moderation.models import ReviewTask as ReviewTaskModel
from app.modules.routes.models import RouteVersion

API = "/api/v1"
EDITOR_ID = "33333333-3333-3333-3333-333333333333"
EDITOR_2_ID = "44444444-4444-4444-4444-444444444444"
USER_ID = "22222222-2222-2222-2222-222222222222"


def _new_key() -> str:
    return str(uuid.uuid4())


def _headers_for(user_id: str, username: str, role: str) -> dict[str, str]:
    token = create_access_token(user_id=user_id, username=username, role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def editor_headers() -> dict[str, str]:
    return _headers_for(EDITOR_ID, "editor_one", "editor")


@pytest.fixture()
def editor2_headers() -> dict[str, str]:
    return _headers_for(EDITOR_2_ID, "editor_two", "editor")


def _post(
    client: TestClient,
    url: str,
    headers: dict[str, str],
    *,
    json: dict[str, Any] | None = None,
) -> Any:
    return client.post(url, headers={**headers, "Idempotency-Key": _new_key()}, json=json)


def _code(response: Any) -> str | None:
    body = response.json()
    return (body.get("error") or {}).get("code")


def _create_draft(client: TestClient, headers: dict[str, str], *, title: str = "城市漫步") -> str:
    response = _post(client, f"{API}/route-drafts", headers, json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _submit(
    client: TestClient, headers: dict[str, str], draft_id: str, *, note: str = "请审核"
) -> dict[str, Any]:
    response = _post(client, f"{API}/route-drafts/{draft_id}/submit", headers, json={"note": note})
    assert response.status_code == 201, response.text
    return response.json()


def _task_for(client: TestClient, headers: dict[str, str], submission_id: str) -> dict[str, Any]:
    response = client.get(f"{API}/review/tasks", headers=headers, params={"limit": 50})
    assert response.status_code == 200, response.text
    for task in response.json()["items"]:
        if task["submission_id"] == submission_id:
            return task
    raise AssertionError("review task not found")


def _claim(client: TestClient, headers: dict[str, str], task_id: str) -> dict[str, Any]:
    response = _post(client, f"{API}/review/tasks/{task_id}/claim", headers)
    assert response.status_code == 200, response.text
    return response.json()


def _approve(
    client: TestClient, headers: dict[str, str], submission_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把一条投稿推进到 APPROVED，返回 (submission, task)。"""
    task = _task_for(client, headers, submission_id)
    _claim(client, headers, task["id"])
    response = _post(
        client,
        f"{API}/review/tasks/{task['id']}/decision",
        headers,
        json={"action": "approve"},
    )
    assert response.status_code == 200, response.text
    detail = client.get(f"{API}/submissions/{submission_id}", headers=headers)
    assert detail.json()["state"] == "APPROVED"
    return detail.json(), response.json()


def _publish_payload(submission: dict[str, Any]) -> dict[str, str]:
    return {
        "version_id": submission["version_id"],
        "content_hash": submission["content_hash"],
    }


# ===== 正常流 =====
def test_submit_review_publish_unpublish_happy_path(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)

    assert submission["state"] == "PENDING_REVIEW"
    assert submission["version_id"]
    assert len(submission["content_hash"]) == 64

    draft = client.get(f"{API}/route-drafts/{draft_id}", headers=user_headers)
    assert draft.status_code == 200
    assert draft.json()["state"] == "SUBMITTED_LOCKED"

    task = _task_for(client, editor_headers, submission["id"])
    assert task["state"] == "OPEN"

    claimed = _claim(client, editor_headers, task["id"])
    assert claimed["state"] == "CLAIMED"
    assert claimed["assignee_id"] == EDITOR_ID
    assert claimed["lease_expires_at"] is not None

    decided = _post(
        client,
        f"{API}/review/tasks/{task['id']}/decision",
        editor_headers,
        json={"action": "approve"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "COMPLETED"
    assert decided.json()["completed_action"] == "approve"
    assert decided.json()["lease_expires_at"] is None

    detail = client.get(f"{API}/submissions/{submission['id']}", headers=user_headers)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["state"] == "APPROVED"
    assert detail_body["snapshot"]["title"] == "城市漫步"
    assert detail_body["previous_snapshot"] is None
    # 草稿没加节点，映射为空但字段必须在：审核页靠它把 place_id 翻成地名
    assert detail_body["place_names"] == {}
    assert detail_body["author"]["username"] == "xiaoming"

    assert (
        client.get(f"{API}/route-drafts/{draft_id}", headers=user_headers).json()["state"]
        == "APPROVED_LOCKED"
    )

    key = _new_key()
    payload = _publish_payload(detail_body)
    first = client.post(
        f"{API}/submissions/{submission['id']}/publish",
        headers={**editor_headers, "Idempotency-Key": key},
        json=payload,
    )
    assert first.status_code == 201, first.text
    publication = first.json()
    assert publication["state"] == "PUBLISHED"
    assert publication["published_at"] is not None
    assert publication["route_id"]

    replay = client.post(
        f"{API}/submissions/{submission['id']}/publish",
        headers={**editor_headers, "Idempotency-Key": key},
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == publication["id"]

    again = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json=payload,
    )
    assert again.status_code == 409
    assert _code(again) == "ROUTE_ALREADY_PUBLISHED"

    unpublished = _post(
        client,
        f"{API}/publications/{publication['id']}/unpublish",
        editor_headers,
        json={"reason": "内容需要修订后重新上线"},
    )
    assert unpublished.status_code == 200, unpublished.text
    assert unpublished.json()["state"] == "UNPUBLISHED"
    assert unpublished.json()["unpublished_reason"] == "内容需要修订后重新上线"

    actions = set(db_session.scalars(select(AuditLog.action)).all())
    assert {
        "submission.submit",
        "review.claim",
        "review.approve",
        "publication.publish",
        "publication.unpublish",
    } <= actions


# ===== MD-04 验收：下线后公共读取必须 404 =====
def test_unpublish_removes_public_detail_and_list_entry(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    """发布后公共详情 200；下线后同一地址 404，公开列表里也不再出现。"""
    draft_id = _create_draft(client, user_headers, title="下线验收路线")
    submission = _submit(client, user_headers, draft_id)
    approved, _task = _approve(client, editor_headers, submission["id"])
    published = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json=_publish_payload(approved),
    )
    assert published.status_code == 201, published.text
    publication = published.json()
    route_id = publication["route_id"]

    detail = client.get(f"{API}/routes/{route_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == route_id
    assert detail.json()["version_id"] == publication["version_id"]

    listed = client.get(f"{API}/routes", params={"limit": 50})
    assert listed.status_code == 200, listed.text
    assert route_id in {item["id"] for item in listed.json()["items"]}

    unpublished = _post(
        client,
        f"{API}/publications/{publication['id']}/unpublish",
        editor_headers,
        json={"reason": "下线后公共详情必须 404"},
    )
    assert unpublished.status_code == 200, unpublished.text
    assert unpublished.json()["state"] == "UNPUBLISHED"

    # 下线只改 publications.state，版本行的 route_id 仍在：公共读取必须按发布态判 404。
    gone = client.get(f"{API}/routes/{route_id}")
    assert gone.status_code == 404, gone.text
    assert _code(gone) == "ROUTE_NOT_FOUND"

    listed_after = client.get(f"{API}/routes", params={"limit": 50})
    assert listed_after.status_code == 200, listed_after.text
    assert route_id not in {item["id"] for item in listed_after.json()["items"]}


# ===== MD-01 验收：提交事务失败要整体回滚 =====
def test_submit_rolls_back_when_audit_write_fails(
    db_session: Session,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结版本 / 建投稿 / 建审核任务 / 写审计里最后一步失败，一个事务整体回滚。

    conftest 的 get_db 覆盖只共享会话、不做提交与回滚，这里临时换回生产语义
    （成功 commit、异常 rollback），否则「回滚」这一步根本不会被执行；
    同时用 raise_server_exceptions=False 才能读到 500 的统一错误信封。
    """

    def _prod_like_override() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _prod_like_override
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            draft_id = _create_draft(test_client, user_headers)
            before = test_client.get(f"{API}/route-drafts/{draft_id}", headers=user_headers)
            assert before.status_code == 200, before.text
            before_body = before.json()
            assert before_body["state"] == "EDITING"

            def _count(model: type) -> int:
                return int(db_session.scalar(select(func.count()).select_from(model)) or 0)

            models = (RouteVersion, Submission, ReviewTaskModel, AuditLog)
            counts_before = {model: _count(model) for model in models}

            def _explode(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("审计写入失败（测试注入）")

            monkeypatch.setattr(AuditService, "write", _explode)
            response = _post(
                test_client,
                f"{API}/route-drafts/{draft_id}/submit",
                user_headers,
                json={"note": "请审核"},
            )
            assert response.status_code == 500, response.text
            assert _code(response) == "INTERNAL_ERROR"

            # 版本 / 投稿 / 审核任务 / 审计：一条脏数据都不能留下。
            assert {model: _count(model) for model in models} == counts_before

            after = test_client.get(f"{API}/route-drafts/{draft_id}", headers=user_headers)
            assert after.status_code == 200, after.text
            after_body = after.json()
            assert after_body["state"] == "EDITING"
            assert after_body["lock_version"] == before_body["lock_version"]

            # 回滚干净后草稿仍可继续提交（重试一次能成功）。
            monkeypatch.undo()
            resubmit = _post(
                test_client,
                f"{API}/route-drafts/{draft_id}/submit",
                user_headers,
                json={"note": "回滚后重试"},
            )
            assert resubmit.status_code == 201, resubmit.text
            assert resubmit.json()["state"] == "PENDING_REVIEW"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ===== 幂等 =====
def test_submit_is_idempotent_and_rejects_key_reuse(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    key = _new_key()
    url = f"{API}/route-drafts/{draft_id}/submit"
    headers = {**user_headers, "Idempotency-Key": key}

    first = client.post(url, headers=headers, json={"note": "第一次提交"})
    assert first.status_code == 201, first.text

    replay = client.post(url, headers=headers, json={"note": "第一次提交"})
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["version_id"] == first.json()["version_id"]
    assert db_session.scalar(select(func.count()).select_from(Submission)) == 1

    reused = client.post(url, headers=headers, json={"note": "换了请求体"})
    assert reused.status_code == 409
    assert _code(reused) == "IDEMPOTENCY_KEY_REUSED"


# ===== 越权 / 非法状态 =====
def test_submit_and_withdraw_require_author(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    forbidden_submit = _post(
        client, f"{API}/route-drafts/{draft_id}/submit", editor_headers, json={}
    )
    assert forbidden_submit.status_code == 403
    assert _code(forbidden_submit) == "DRAFT_FORBIDDEN"

    editors_draft = _create_draft(client, editor_headers, title="别人的草稿")
    forbidden_withdraw = _post(client, f"{API}/route-drafts/{editors_draft}/withdraw", user_headers)
    assert forbidden_withdraw.status_code == 403
    assert _code(forbidden_withdraw) == "DRAFT_FORBIDDEN"

    submittable = _create_draft(client, user_headers, title="提交后再撤回")
    _submit(client, user_headers, submittable)
    withdrawn = _post(client, f"{API}/route-drafts/{submittable}/withdraw", user_headers)
    assert withdrawn.status_code == 200
    illegal = _post(client, f"{API}/route-drafts/{submittable}/withdraw", user_headers)
    assert illegal.status_code == 409
    assert _code(illegal) == "DRAFT_NOT_SUBMITTED"


def test_withdraw_rejected_while_task_claimed(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])

    response = _post(client, f"{API}/route-drafts/{draft_id}/withdraw", user_headers)
    assert response.status_code == 409
    assert _code(response) == "REVIEW_TASK_CLAIMED"


def test_release_then_author_can_withdraw(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])

    released = _post(client, f"{API}/review/tasks/{task['id']}/release", editor_headers)
    assert released.status_code == 200, released.text
    assert released.json()["state"] == "OPEN"
    assert released.json()["assignee_id"] is None

    withdrawn = _post(client, f"{API}/route-drafts/{draft_id}/withdraw", user_headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["state"] == "EDITING"

    stored = db_session.get(ReviewTaskModel, uuid.UUID(task["id"]))
    assert stored is not None
    assert stored.state.value == "CANCELLED"
    assert stored.cancel_reason == "作者撤回投稿"

    detail = client.get(f"{API}/submissions/{submission['id']}", headers=user_headers)
    assert detail.json()["state"] == "WITHDRAWN"


def test_withdraw_allowed_after_lease_expired(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])

    stored = db_session.get(ReviewTaskModel, uuid.UUID(task["id"]))
    assert stored is not None
    stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    withdrawn = _post(client, f"{API}/route-drafts/{draft_id}/withdraw", user_headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["state"] == "EDITING"
    db_session.refresh(stored)
    assert stored.state.value == "CANCELLED"


def test_expired_lease_can_be_reclaimed_by_another_reviewer(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
    editor2_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])

    stored = db_session.get(ReviewTaskModel, uuid.UUID(task["id"]))
    assert stored is not None
    stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    reclaimed = _claim(client, editor2_headers, task["id"])
    assert reclaimed["state"] == "CLAIMED"
    assert reclaimed["assignee_id"] == EDITOR_2_ID


# ===== 发布校验 =====
def test_publish_requires_approval_and_matching_hash(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)

    too_early = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json=_publish_payload(submission),
    )
    assert too_early.status_code == 409
    assert _code(too_early) == "SUBMISSION_NOT_APPROVED"

    detail, _task = _approve(client, editor_headers, submission["id"])
    approved = detail
    wrong_hash = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json={"version_id": approved["version_id"], "content_hash": "0" * 64},
    )
    assert wrong_hash.status_code == 409
    assert _code(wrong_hash) == "CONTENT_HASH_MISMATCH"

    wrong_version = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json={"version_id": str(uuid.uuid4()), "content_hash": approved["content_hash"]},
    )
    assert wrong_version.status_code == 409
    assert _code(wrong_version) == "VERSION_MISMATCH"


def test_unpublish_requires_published_state_and_valid_reason(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    approved, _task = _approve(client, editor_headers, submission["id"])
    published = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json=_publish_payload(approved),
    )
    assert published.status_code == 201, published.text
    publication_id = published.json()["id"]

    too_short = _post(
        client,
        f"{API}/publications/{publication_id}/unpublish",
        editor_headers,
        json={"reason": "短"},
    )
    assert too_short.status_code == 422

    first = _post(
        client,
        f"{API}/publications/{publication_id}/unpublish",
        editor_headers,
        json={"reason": "内容需要修订后重新上线"},
    )
    assert first.status_code == 200, first.text

    second = _post(
        client,
        f"{API}/publications/{publication_id}/unpublish",
        editor_headers,
        json={"reason": "重复下线同一发布记录"},
    )
    assert second.status_code == 409
    assert _code(second) == "PUBLICATION_NOT_PUBLISHED"


# ===== 审核权限 / 利益冲突 =====
def test_review_requires_reviewer_role_and_assignee(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
    editor2_headers: dict[str, str],
) -> None:
    forbidden = client.get(f"{API}/review/tasks", headers=user_headers)
    assert forbidden.status_code == 403

    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])

    unclaimed = _post(
        client,
        f"{API}/review/tasks/{task['id']}/decision",
        editor_headers,
        json={"action": "approve"},
    )
    assert unclaimed.status_code == 409
    assert _code(unclaimed) == "TASK_NOT_CLAIMED"

    too_short = _post(
        client,
        f"{API}/review/tasks/{task['id']}/decision",
        editor_headers,
        json={"action": "request_changes", "reason": "太短"},
    )
    assert too_short.status_code == 422

    _claim(client, editor_headers, task["id"])
    wrong_reviewer = _post(
        client,
        f"{API}/review/tasks/{task['id']}/decision",
        editor2_headers,
        json={"action": "approve"},
    )
    assert wrong_reviewer.status_code == 403
    assert _code(wrong_reviewer) == "NOT_TASK_ASSIGNEE"


def test_cannot_review_own_submission(
    client: TestClient,
    editor_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, editor_headers, title="审核员自己的草稿")
    submission = _submit(client, editor_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])

    conflict = _post(
        client,
        f"{API}/review/tasks/{task['id']}/decision",
        editor_headers,
        json={"action": "approve"},
    )
    assert conflict.status_code == 403
    assert _code(conflict) == "SELF_REVIEW_FORBIDDEN"


# ===== 第二批：租约过期的 SQLite 时区 / 发布唯一性 =====
def test_claim_after_lease_expiry_survives_naive_datetime_readback(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
    editor2_headers: dict[str, str],
) -> None:
    """租约过期后再领取，不能因为 SQLite 读回 naive 时间就 500。

    UPDATE 的 WHERE 引用了库里的 lease_expires_at：ORM 默认会在 Python 里重算它
    （synchronize_session="auto"），而 SQLite 把 DateTime(timezone=True) 读回来是
    naive 的，与 aware 的 now 一比就 TypeError。expire_all() 强制从库里重读，
    复现真实请求里「另一个进程刚写过这一行」的情形。
    """
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])

    db_session.execute(
        update(ReviewTaskModel)
        .where(ReviewTaskModel.id == uuid.UUID(task["id"]))
        .values(lease_expires_at=datetime.now(UTC) - timedelta(minutes=5))
    )
    db_session.commit()
    db_session.expire_all()

    stolen = _post(client, f"{API}/review/tasks/{task['id']}/claim", editor2_headers)

    assert stolen.status_code == 200, stolen.text
    assert stolen.json()["assignee_id"] == EDITOR_2_ID


def test_second_claim_before_expiry_is_conflict_not_server_error(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
    editor2_headers: dict[str, str],
) -> None:
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    task = _task_for(client, editor_headers, submission["id"])
    _claim(client, editor_headers, task["id"])
    db_session.expire_all()

    conflict = _post(client, f"{API}/review/tasks/{task['id']}/claim", editor2_headers)

    assert conflict.status_code == 409
    assert _code(conflict) == "TASK_ALREADY_CLAIMED"


def test_publication_partial_unique_index_exists(db_session: Session) -> None:
    """docs/02 §5 的「一条路线只有一条 PUBLISHED」必须由库兜底，不能只靠先查后插。"""
    sql = db_session.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'uq_publications_route_published'"
        )
    ).scalar_one()

    assert "WHERE state = 'PUBLISHED'" in sql


def test_second_published_row_for_same_route_is_rejected_by_index(db_session: Session) -> None:
    route_id = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            Publication(
                submission_id=uuid.uuid4(),
                route_id=route_id,
                version_id=uuid.uuid4(),
                content_hash="c" * 64,
                state=PublicationState.PUBLISHED,
            )
        )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_first_publish_route_id_is_deterministic(
    client: TestClient,
    user_headers: dict[str, str],
    editor_headers: dict[str, str],
) -> None:
    """首次发布的 route_id 由 draft_id 派生：并发的两次首发必须落到同一个 route。"""
    draft_id = _create_draft(client, user_headers)
    submission = _submit(client, user_headers, draft_id)
    detail, _task = _approve(client, editor_headers, submission["id"])

    response = _post(
        client,
        f"{API}/submissions/{submission['id']}/publish",
        editor_headers,
        json=_publish_payload(detail),
    )

    assert response.status_code == 201, response.text
    assert response.json()["route_id"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"culturalwalk:route-draft:{draft_id}")
    )
