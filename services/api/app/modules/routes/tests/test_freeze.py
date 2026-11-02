"""RT-03：草稿冻结成不可变版本 + 内容 hash。

验收（docs/01 示例一）：
1) 同一草稿冻结两次，得到两个 version_id，hash 相同；
2) 改一个节点再冻结，hash 不同；
3) 冻结后修改草稿，已生成的 version 内容不变。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.modules.routes.models import RouteDraft, RouteNode
from app.modules.routes.service import RoutesService

ACTOR = uuid.UUID("22222222-2222-2222-2222-222222222222")
API = "/api/v1/route-drafts"


def _draft(db: Session, *, nodes: int = 2) -> RouteDraft:
    draft = RouteDraft(owner_id=ACTOR, title="岳麓山半日", summary="上山再到江边")
    db.add(draft)
    db.flush()
    for index in range(1, nodes + 1):
        db.add(
            RouteNode(
                draft_id=draft.id,
                place_id=uuid.uuid4(),
                position=index,
                stay_minutes=30 * index,
                transport="walk",
                note=f"第 {index} 站",
                price_min=1000 * index,
                price_max=2000 * index,
            )
        )
    db.flush()
    return draft


def test_freeze_twice_yields_two_versions_with_same_hash(db_session: Session) -> None:
    draft = _draft(db_session, nodes=2)
    service = RoutesService(db_session)

    first = service.freeze_version(draft_id=draft.id, actor_id=ACTOR)
    second = service.freeze_version(draft_id=draft.id, actor_id=ACTOR)

    assert first.id != second.id
    assert (first.version_no, second.version_no) == (1, 2)
    # 内容没变 → 同一份内容 hash（docs/02：sha256(canonical_json(snapshot))）。
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64


def test_changing_a_node_changes_hash(db_session: Session) -> None:
    draft = _draft(db_session, nodes=2)
    service = RoutesService(db_session)
    before = service.freeze_version(draft_id=draft.id, actor_id=ACTOR)

    node = db_session.query(RouteNode).filter_by(draft_id=draft.id, position=2).one()
    node.note = "改过的备注"
    node.price_max = 9999
    db_session.flush()

    after = service.freeze_version(draft_id=draft.id, actor_id=ACTOR)
    assert after.content_hash != before.content_hash


def test_frozen_snapshot_is_immutable_after_draft_edit(db_session: Session) -> None:
    draft = _draft(db_session, nodes=1)
    service = RoutesService(db_session)
    version = service.freeze_version(draft_id=draft.id, actor_id=ACTOR)
    original = version.snapshot

    draft.title = "改过的标题"
    db_session.query(RouteNode).filter_by(draft_id=draft.id).one().note = "改过的备注"
    db_session.flush()
    db_session.refresh(version)

    assert version.snapshot == original
    assert version.snapshot["title"] == "岳麓山半日"
    assert version.snapshot["nodes"][0]["note"] == "第 1 站"


def test_snapshot_lists_nodes_in_position_order(db_session: Session) -> None:
    draft = _draft(db_session, nodes=3)
    service = RoutesService(db_session)
    version = service.freeze_version(draft_id=draft.id, actor_id=ACTOR)

    positions = [node["position"] for node in version.snapshot["nodes"]]
    assert positions == [1, 2, 3]
    assert set(version.snapshot) == {
        "title",
        "summary",
        "theme_tags",
        "duration_minutes",
        "nodes",
    }


def test_version_no_starts_at_one_for_each_draft(db_session: Session) -> None:
    service = RoutesService(db_session)
    first_draft = _draft(db_session, nodes=1)
    second_draft = _draft(db_session, nodes=1)

    assert service.freeze_version(draft_id=first_draft.id, actor_id=ACTOR).version_no == 1
    assert service.freeze_version(draft_id=first_draft.id, actor_id=ACTOR).version_no == 2
    assert service.freeze_version(draft_id=second_draft.id, actor_id=ACTOR).version_no == 1


def test_freeze_unknown_draft_raises_not_found(db_session: Session) -> None:
    service = RoutesService(db_session)
    try:
        service.freeze_version(draft_id=uuid.uuid4(), actor_id=ACTOR)
    except NotFound as exc:
        assert exc.code == "DRAFT_NOT_FOUND"
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("应当抛 DRAFT_NOT_FOUND")


def test_delete_draft_with_versions_returns_conflict(
    client: TestClient,
    db_session: Session,
    user_headers: dict[str, str],
) -> None:
    draft = _draft(db_session, nodes=1)
    RoutesService(db_session).freeze_version(draft_id=draft.id, actor_id=ACTOR)
    db_session.flush()

    response = client.delete(f"{API}/{draft.id}", headers=user_headers)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DRAFT_HAS_VERSIONS"
