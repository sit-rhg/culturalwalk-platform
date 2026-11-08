"""MD-03 × NT-02 联动：走真实裁决流程，确认通知真的写出来。

notifications 模块内的测试直接调 notify()，链路断了也照样绿；这里从
POST /review/tasks/{id}/decision 出发，经 decide() 写通知，再用作者身份
GET /notifications 验证。顺带覆盖 MD-03「退回/拒绝必须带理由」的负例。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.deps import create_access_token

API = "/api/v1"
EDITOR_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def editor_headers() -> dict[str, str]:
    token = create_access_token(user_id=EDITOR_ID, username="editor_one", role="editor")
    return {"Authorization": f"Bearer {token}"}


def _new_key() -> str:
    return str(uuid.uuid4())


def _post(
    client: TestClient,
    url: str,
    headers: dict[str, str],
    *,
    json: dict[str, Any] | None = None,
) -> Any:
    return client.post(url, headers={**headers, "Idempotency-Key": _new_key()}, json=json)


def _draft_and_submission(
    client: TestClient, user_headers: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    draft = _post(client, f"{API}/route-drafts", user_headers, json={"title": "城市漫步"})
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    submission = _post(
        client,
        f"{API}/route-drafts/{draft_id}/submit",
        user_headers,
        json={"note": "请审核"},
    )
    assert submission.status_code == 201, submission.text
    return draft_id, submission.json()


def _claim_task(client: TestClient, editor_headers: dict[str, str], submission_id: str) -> str:
    response = client.get(f"{API}/review/tasks", headers=editor_headers, params={"limit": 50})
    assert response.status_code == 200, response.text
    task = next(item for item in response.json()["items"] if item["submission_id"] == submission_id)
    claimed = _post(client, f"{API}/review/tasks/{task['id']}/claim", editor_headers)
    assert claimed.status_code == 200, claimed.text
    return task["id"]


def _decide(
    client: TestClient,
    editor_headers: dict[str, str],
    task_id: str,
    body: dict[str, Any],
) -> Any:
    return _post(client, f"{API}/review/tasks/{task_id}/decision", editor_headers, json=body)


def _notifications(client: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = client.get(f"{API}/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_approve_writes_notification_for_author(
    client: TestClient, user_headers: dict[str, str], editor_headers: dict[str, str]
) -> None:
    _, submission = _draft_and_submission(client, user_headers)
    task_id = _claim_task(client, editor_headers, submission["id"])

    response = _decide(client, editor_headers, task_id, {"action": "approve"})
    assert response.status_code == 200, response.text

    items = _notifications(client, user_headers)
    assert [item["type"] for item in items] == ["review_approved"]
    payload = items[0]["payload"]
    assert payload["submission_id"] == submission["id"]
    assert payload["title"] == "城市漫步"
    assert payload["decision"] == "approve"
    # 通过不需要理由，payload 里不该出现空字符串占位。
    assert "reason" not in payload


def test_request_changes_carries_trimmed_reason(
    client: TestClient, user_headers: dict[str, str], editor_headers: dict[str, str]
) -> None:
    _, submission = _draft_and_submission(client, user_headers)
    task_id = _claim_task(client, editor_headers, submission["id"])

    response = _decide(
        client,
        editor_headers,
        task_id,
        {"action": "request_changes", "reason": "  节点太少，至少补到 3 个  "},
    )
    assert response.status_code == 200, response.text

    items = _notifications(client, user_headers)
    assert [item["type"] for item in items] == ["review_request_changes"]
    # 前端 latestReviewReason() 直接渲染 payload.reason，首尾空格要先去掉。
    assert items[0]["payload"]["reason"] == "节点太少，至少补到 3 个"


def test_reject_writes_notification_with_reason(
    client: TestClient, user_headers: dict[str, str], editor_headers: dict[str, str]
) -> None:
    _, submission = _draft_and_submission(client, user_headers)
    task_id = _claim_task(client, editor_headers, submission["id"])

    response = _decide(
        client,
        editor_headers,
        task_id,
        {"action": "reject", "reason": "内容与城市漫步主题无关"},
    )
    assert response.status_code == 200, response.text

    items = _notifications(client, user_headers)
    assert [item["type"] for item in items] == ["review_rejected"]
    assert items[0]["payload"]["reason"] == "内容与城市漫步主题无关"


def test_missing_reason_is_rejected_before_the_task_is_completed(
    client: TestClient, user_headers: dict[str, str], editor_headers: dict[str, str]
) -> None:
    """负例：没有理由就退回，422 且状态一步都不能动。

    校验必须在 complete_cas 之前：先判 422 再改状态，否则用户补上理由重试
    只会看到「任务已结束」。
    """
    _, submission = _draft_and_submission(client, user_headers)
    task_id = _claim_task(client, editor_headers, submission["id"])

    response = _decide(client, editor_headers, task_id, {"action": "request_changes"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REVIEW_REASON_REQUIRED"
    assert _notifications(client, user_headers) == []

    detail = client.get(f"{API}/submissions/{submission['id']}", headers=editor_headers)
    assert detail.json()["state"] == "PENDING_REVIEW"

    tasks = client.get(f"{API}/review/tasks", headers=editor_headers, params={"limit": 50})
    current = next(item for item in tasks.json()["items"] if item["id"] == task_id)
    assert current["state"] == "CLAIMED"
    assert current["completed_action"] is None

    # 补上理由后同一条任务还能正常退回，证明 422 没有把任务卡死。
    retry = _decide(
        client,
        editor_headers,
        task_id,
        {"action": "request_changes", "reason": "节点太少，至少补到 3 个"},
    )
    assert retry.status_code == 200, retry.text
    assert [item["type"] for item in _notifications(client, user_headers)] == [
        "review_request_changes"
    ]
