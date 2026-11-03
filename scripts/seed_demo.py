"""演示种子数据：3 个账号 + 长沙 6 个点位 + 3 条路线草稿。

可重复执行：所有行都有稳定主键 / 幂等键，第二次运行不会重复插入，也不会覆盖
后台已经改过的内容（账号密码与角色除外，见下）。

用法（在 services/api 目录下执行；脚本本身只依赖 app 的公开 service）：

    # 首次准备数据库：建到最新迁移（种子脚本不负责迁移）
    DATABASE_URL="sqlite:///./demo.db" .venv/Scripts/python.exe -m alembic upgrade head

    # 灌种子，可反复执行
    .venv/Scripts/python.exe ../../scripts/seed_demo.py --database-url "sqlite:///./demo.db"

PowerShell 等价写法：

    $env:DATABASE_URL="sqlite:///./demo.db"; .venv/Scripts/python.exe -m alembic upgrade head
    .venv/Scripts/python.exe ../../scripts/seed_demo.py --database-url "sqlite:///./demo.db"

`--database-url` 会在 import app 之前写入环境变量 DATABASE_URL，所以本脚本与
alembic / uvicorn 看到的始终是同一个库；不传时沿用环境变量或 app/core/config.py
的默认值（默认是本地 PostgreSQL）。相对路径的 SQLite 文件相对于当前工作目录解析。

造的数据：
- editor@demo.local（editor）、reviewer@demo.local（admin）、demo@demo.local（user），
  密码统一 Demo12345!，只存 argon2id 摘要。仓库没有单独的 reviewer 角色，审核能力
  现有 RBAC 里由 admin 承担，所以审核员账号用 admin。
- catalog/seed_data.json 里的 6 个长沙点位（复用 CA-02 的校验与 uuid5 主键，
  verified 说法必须带来源的红线也在那里）。
- 2 条“已发布”路线 + 1 条待审草稿。注意：仓库当前没有 routes/submissions/
  publications 表，也没有发布、审核队列端点（MD-02/MD-04 未实现），因此这里用
  RouteDraft + 冻结的 RouteVersion 表达：APPROVED_LOCKED 视为已发布，
  SUBMITTED_LOCKED 视为待审。这是现有 schema 能表达的最接近状态，不是真正的
  发布记录——等发布/审核模块落地后应改为调用对应 service。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# 以 `python ../../scripts/seed_demo.py` 运行时，sys.path[0] 是 scripts/，
# 先把 services/api 加回来，后续 import app.* 才能找到包。
REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "services" / "api"
DEFAULT_DATA = API_ROOT / "app" / "modules" / "catalog" / "seed_data.json"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

PASSWORD = "Demo12345!"

# 六个演示点位（slug 与 catalog/seed_data.json 对齐）。
DEMO_SLUGS: tuple[str, ...] = (
    "yuelu-academy",  # 岳麓书院
    "orange-isle",  # 橘子洲
    "taiping-street",  # 太平老街
    "hunan-museum",  # 湖南博物院
    "tianxin-pavilion",  # 天心阁
    "chaozong-street",  # 潮宗街
)

DEMO_USERS: tuple[tuple[str, str, str], ...] = (
    ("editor@demo.local", "editor", "演示编辑"),
    ("reviewer@demo.local", "admin", "演示审核员（现有 RBAC 下审核由 admin 承担）"),
    ("demo@demo.local", "user", "演示普通用户"),
)

# seed_data.json 里这两个点位的说法只有 confidence=likely 且没有 URL；补一条
# verified + 可点击来源，保证 6 个演示点位都满足“来源 URL + 可信度”的演示要求。
# 链接在 2026-11 实测可访问，内容分别是馆方参观信息与潮宗街街区改造报道。
DEMO_FACT_SOURCES: dict[str, dict[str, str]] = {
    "hunan-museum": {
        "claim": "湖南博物院位于长沙市开福区东风路50号，每周二至周日9:00—17:00开放（周一闭馆）。",
        "title": "湖南博物院官网（参观信息）",
        "url": "https://www.hnmuseum.com/",
        "publisher": "湖南博物院",
    },
    "chaozong-street": {
        "claim": (
            "潮宗街是长沙第二条历史文化街区，按“有机更新”模式改造为开放式街区，"
            "计划2019年底完成建设。"
        ),
        "title": "长沙潮宗街历史文化街区2019年完工",
        "url": "http://www.changsha.gov.cn/szf/ywdt/zwdt/201812/t20181203_5635726.html",
        "publisher": "长沙晚报（长沙市政府门户网站转载）",
    },
}


@dataclass(frozen=True, slots=True)
class DemoNode:
    slug: str
    stay_minutes: int
    transport: str | None
    note: str


@dataclass(frozen=True, slots=True)
class DemoRoute:
    key: str
    title: str
    summary: str
    theme_tags: list[str]
    duration_minutes: int
    state: str  # DraftState 的 value；这里用字符串，避免 dataclass 默认值引用 import 顺序
    channel: str  # official=平台精选，user=用户投稿（0011 起落库）
    nodes: tuple[DemoNode, ...]


DEMO_ROUTES: tuple[DemoRoute, ...] = (
    DemoRoute(
        key="demo-seed-route-a-v1",
        title="山水洲城：岳麓书院—橘子洲—太平老街",
        summary="从千年学府到湘江洲头，再回到老城街巷，一条线看完长沙的山水与市井。",
        theme_tags=["历史", "自然", "建筑"],
        duration_minutes=300,
        state="APPROVED_LOCKED",
        channel="official",
        nodes=(
            DemoNode("yuelu-academy", 90, None, "先看讲堂与御书楼，再沿岳麓山脚走一圈。"),
            DemoNode("orange-isle", 120, "metro", "地铁 4 号线到橘子洲站，洲头看湘江北去。"),
            DemoNode("taiping-street", 60, "walk", "傍晚进太平老街，边走边吃。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-b-v1",
        title="老城文脉：天心阁—湖南博物院—潮宗街",
        summary="登古城墙、看马王堆，再钻进潮宗街的巷子，一天走完长沙的旧与新。",
        theme_tags=["历史", "建筑", "民俗"],
        duration_minutes=300,
        state="APPROVED_LOCKED",
        channel="user",
        nodes=(
            DemoNode("tianxin-pavilion", 60, None, "上城墙看老城天际线，约一小时足够。"),
            DemoNode("hunan-museum", 150, "taxi", "马王堆陈列是重点，建议提前预约。"),
            DemoNode("chaozong-street", 60, "walk", "从博物院打车到潮宗街，慢走收尾。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-pending-v1",
        title="老城漫步：太平老街—潮宗街—天心阁",
        summary="待审核示例：三个老城点位串联，用于演示审核队列里的待审草稿。",
        theme_tags=["历史", "民俗", "美食"],
        duration_minutes=180,
        state="SUBMITTED_LOCKED",
        channel="user",
        nodes=(
            DemoNode("taiping-street", 60, None, "从五一大道口进老街。"),
            DemoNode("chaozong-street", 90, "walk", "沿湘江路向北步行到潮宗街。"),
            DemoNode("tianxin-pavilion", 45, "metro", "地铁 1 号线到南门口，步行上天心阁。"),
        ),
    ),
)


def _configure_console() -> None:
    """Windows 控制台默认 GBK，中文与破折号会变成乱码甚至抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _database_url_from_argv(argv: list[str]) -> str | None:
    """在 import app 之前先摸出 --database-url，保证 engine 用对库。"""
    for index, arg in enumerate(argv):
        if arg == "--database-url" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--database-url="):
            return arg.split("=", 1)[1]
    return None


_url = _database_url_from_argv(sys.argv[1:])
if _url:
    os.environ["DATABASE_URL"] = _url

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.modules.catalog.models import (  # noqa: E402
    FactConfidence,
    Place,
    PlaceFact,
    PlaceSource,
)
from app.modules.catalog.seed import (  # noqa: E402
    apply_seed,
    load_seed_places,
    place_id,
    source_id,
)
from app.modules.identity.models import STATUS_ACTIVE, User  # noqa: E402
from app.modules.identity.service import hash_password, verify_password  # noqa: E402
from app.modules.moderation.models import (  # noqa: E402
    Publication,
    PublicationState,
    ReviewTask,
    ReviewTaskState,
    Submission,
    SubmissionState,
)
from app.modules.routes.models import DraftState, RouteDraft, RouteVersion  # noqa: E402
from app.modules.routes.schemas import (  # noqa: E402
    RouteDraftCreate,
    RouteNodeInput,
    RouteNodesReplace,
)
from app.modules.routes.service import RoutesService  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="灌入 CulturalWalk 演示种子数据")
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖 DATABASE_URL，例如 sqlite:///./demo.db（须在 import app 前生效）",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help=f"catalog 种子数据路径（默认 {DEFAULT_DATA}）",
    )
    return parser.parse_args(argv)


def _demo_user_id(username: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"culturalwalk:demo:user:{username}")


def _ensure_user(session, username: str, role: str, bio: str) -> tuple[User, bool]:
    """按 username（lower 唯一索引）查重；已存在则只纠正角色/密码，不新增行。"""
    user = session.scalar(select(User).where(func.lower(User.username) == username))
    if user is not None:
        if user.role != role:
            user.role = role
        if user.status != STATUS_ACTIVE:
            user.status = STATUS_ACTIVE
        if not verify_password(user.password_hash, PASSWORD):
            user.password_hash = hash_password(PASSWORD)
        return user, False

    user = User(
        id=_demo_user_id(username),
        username=username,
        email=username,
        password_hash=hash_password(PASSWORD),
        role=role,
        status=STATUS_ACTIVE,
        bio=bio,
    )
    session.add(user)
    session.flush()
    return user, True


def _ensure_demo_fact(session, slug: str) -> tuple[bool, bool]:
    """给缺少 URL 来源的点位补一条 verified 说法；返回 (来源是否新增, 说法是否新增)。"""
    spec = DEMO_FACT_SOURCES.get(slug)
    if spec is None:
        return False, False

    pid = place_id(slug)
    if session.get(Place, pid) is None:
        raise RuntimeError(f"点位 {slug} 不在库里，先让 catalog 种子成功落库")

    sid = source_id(spec["title"], spec["url"])
    source_created = False
    if session.get(PlaceSource, sid) is None:
        session.add(
            PlaceSource(
                id=sid,
                title=spec["title"],
                url=spec["url"],
                publisher=spec["publisher"],
                accessed_at=datetime.now(UTC),
            )
        )
        session.flush()
        source_created = True

    fid = uuid.uuid5(uuid.NAMESPACE_URL, f"culturalwalk:demo:fact:{slug}:source-url")
    fact_created = False
    if session.get(PlaceFact, fid) is None:
        session.add(
            PlaceFact(
                id=fid,
                place_id=pid,
                claim=spec["claim"],
                confidence=FactConfidence.VERIFIED,
                source_id=sid,
            )
        )
        session.flush()
        fact_created = True
    return source_created, fact_created


def _route_input_nodes(spec: DemoRoute) -> RouteNodesReplace:
    return RouteNodesReplace(
        nodes=[
            RouteNodeInput(
                place_id=place_id(node.slug),
                stay_minutes=node.stay_minutes,
                transport=node.transport,
                note=node.note,
            )
            for node in spec.nodes
        ]
    )


def _ensure_route(session, editor: User, spec: DemoRoute) -> dict[str, object]:
    """用 RoutesService 建草稿、写节点、冻结版本，最后落到演示状态。"""
    service = RoutesService(session)
    existing = session.scalar(
        select(RouteDraft).where(
            RouteDraft.owner_id == editor.id,
            RouteDraft.idempotency_key == spec.key,
        )
    )
    draft = service.create(
        owner_id=editor.id,
        payload=RouteDraftCreate(
            title=spec.title,
            summary=spec.summary,
            theme_tags=spec.theme_tags,
            duration_minutes=spec.duration_minutes,
        ),
        idempotency_key=spec.key,
    )
    # 来源通道不进契约（RouteDraftCreate 没有这个字段），演示数据自己落：
    # 必须在 freeze_version 之前设好，否则快照里没有 channel，发布后会被当成用户投稿。
    if draft.channel != spec.channel:
        draft.channel = spec.channel
        session.flush()

    # 同 key 重放时 replace_nodes 会在写库前直接返回，所以这里第二遍也是安全的。
    service.replace_nodes(
        draft_id=draft.id,
        owner_id=editor.id,
        payload=_route_input_nodes(spec),
        if_match=draft.lock_version,
        idempotency_key=f"{spec.key}:nodes",
    )

    has_version = session.scalar(
        select(RouteVersion.id).where(RouteVersion.draft_id == draft.id).limit(1)
    )
    version_created = False
    if has_version is None:
        service.freeze_version(draft_id=draft.id, actor_id=editor.id)
        version_created = True

    target_state = DraftState(spec.state)
    state_changed = draft.state is not target_state
    if state_changed:
        # 真实状态流转应该由 MD-01/MD-04 的 service 完成；这两个模块还没实现，
        # 演示数据只能直接落状态，报告里已说明这是临时表达。
        draft.state = target_state

    return {
        "created": existing is None,
        "version_created": version_created,
        "state_changed": state_changed,
        "draft": draft,
    }


def _ensure_published(session, spec: DemoRoute, draft: RouteDraft, editor: User) -> bool:
    """给 APPROVED_LOCKED 的演示草稿补上投稿与发布记录。

    公开列表读的是「有发布记录的路线」，只有 RouteDraft.state 还不够：没有
    submission / publication 行，首页发现页就是空的（台账 P-19）。
    """
    if DraftState(spec.state) is not DraftState.APPROVED_LOCKED:
        return False

    version = session.scalar(
        select(RouteVersion)
        .where(RouteVersion.draft_id == draft.id)
        .order_by(RouteVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        raise RuntimeError(f"演示路线 {spec.key} 没有冻结版本，先跑 _ensure_route")

    if draft.route_id is None:
        draft.route_id = uuid.uuid4()
        session.execute(
            update(RouteVersion)
            .where(RouteVersion.draft_id == draft.id, RouteVersion.route_id.is_(None))
            .values(route_id=draft.route_id)
        )
        session.flush()

    submission = session.scalar(
        select(Submission).where(
            Submission.draft_id == draft.id, Submission.version_id == version.id
        )
    )
    if submission is None:
        submission = Submission(
            draft_id=draft.id,
            route_id=draft.route_id,
            version_id=version.id,
            content_hash=version.content_hash,
            submitted_by=editor.id,
            state=SubmissionState.APPROVED,
            note="演示种子：直接落终态，审核过程不重放",
        )
        session.add(submission)
        session.flush()

    exists = session.scalar(select(Publication.id).where(Publication.version_id == version.id))
    if exists is not None:
        return False

    session.add(
        Publication(
            submission_id=submission.id,
            route_id=draft.route_id,
            version_id=version.id,
            content_hash=version.content_hash,
            state=PublicationState.PUBLISHED,
            published_by=editor.id,
            published_at=datetime.now(UTC),
        )
    )
    session.flush()
    return True


def _ensure_for_review(session, spec: DemoRoute, draft: RouteDraft, editor: User) -> bool:
    """给 SUBMITTED_LOCKED 的演示草稿补上投稿与审核任务。

    审核后台读的是 submissions + review_tasks 两张表；只把草稿状态置成
    SUBMITTED_LOCKED，审核队列仍然是 0 条（和发现页为空是同一类问题，见台账 P-19）。
    """
    if DraftState(spec.state) is not DraftState.SUBMITTED_LOCKED:
        return False

    version = session.scalar(
        select(RouteVersion)
        .where(RouteVersion.draft_id == draft.id)
        .order_by(RouteVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        raise RuntimeError(f"演示路线 {spec.key} 没有冻结版本，先跑 _ensure_route")

    submission = session.scalar(
        select(Submission).where(
            Submission.draft_id == draft.id, Submission.version_id == version.id
        )
    )
    if submission is None:
        submission = Submission(
            draft_id=draft.id,
            version_id=version.id,
            content_hash=version.content_hash,
            submitted_by=editor.id,
            state=SubmissionState.PENDING_REVIEW,
            note="演示种子：待审投稿，演示审核后台的领取与裁决",
        )
        session.add(submission)
        session.flush()

    exists = session.scalar(select(ReviewTask.id).where(ReviewTask.submission_id == submission.id))
    if exists is not None:
        return False

    session.add(ReviewTask(submission_id=submission.id, state=ReviewTaskState.OPEN))
    session.flush()
    return True


def _print_counts(session, place_ids: list[uuid.UUID]) -> None:
    fact_count = session.scalar(
        select(func.count()).select_from(PlaceFact).where(PlaceFact.place_id.in_(place_ids))
    )
    sourced = session.scalar(
        select(func.count())
        .select_from(PlaceFact)
        .join(PlaceSource, PlaceFact.source_id == PlaceSource.id)
        .where(PlaceFact.place_id.in_(place_ids), PlaceSource.url.is_not(None))
    )
    print(f"[校验] 6 个点位共 {fact_count} 条说法，其中 {sourced} 条带可点击来源 URL")


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _parse_args(argv)
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    url = make_url(settings.database_url)
    print(f"[环境] 数据库：{url.render_as_string(hide_password=True)}")

    try:
        seed_places = load_seed_places(args.data)
    except Exception as exc:  # noqa: BLE001 - 顶层统一转成退出码
        print(
            f"[失败] 读不了/校验不过 catalog 种子数据：{type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 1

    by_slug = {place.slug: place for place in seed_places}
    missing = [slug for slug in DEMO_SLUGS if slug not in by_slug]
    if missing:
        print(f"[失败] catalog 种子数据缺少演示点位：{missing}", file=sys.stderr)
        return 1
    selected = [by_slug[slug] for slug in DEMO_SLUGS]
    place_ids = [place_id(slug) for slug in DEMO_SLUGS]

    session = SessionLocal()
    try:
        users_created = 0
        users = {}
        for username, role, bio in DEMO_USERS:
            user, created = _ensure_user(session, username, role, bio)
            users[role] = user
            users_created += int(created)
        editor = users["editor"]

        report = apply_seed(session, selected)

        sources_created = 0
        facts_created = 0
        for slug in DEMO_SLUGS:
            source_added, fact_added = _ensure_demo_fact(session, slug)
            sources_created += int(source_added)
            facts_created += int(fact_added)

        route_stats = []
        published_new = 0
        pending_new = 0
        for spec in DEMO_ROUTES:
            stats = _ensure_route(session, editor, spec)
            published_new += int(_ensure_published(session, spec, stats["draft"], editor))
            pending_new += int(_ensure_for_review(session, spec, stats["draft"], editor))
            route_stats.append((spec, stats))

        session.commit()

        print(
            f"[种子] 用户：新增 {users_created} / 共 {len(DEMO_USERS)}"
            f"（editor、admin、user 各 1，密码 {PASSWORD}）"
        )
        print(
            f"[种子] 地点：新增 {report.inserted.get('places', 0)} / 共 {len(selected)}；"
            f"来源新增 {report.inserted.get('sources', 0) + sources_created}；"
            f"说法新增 {report.inserted.get('facts', 0) + facts_created}"
        )
        _print_counts(session, place_ids)
        node_total = sum(len(spec.nodes) for spec in DEMO_ROUTES)
        print(f"[种子] 路线：{len(DEMO_ROUTES)} 条（节点 {node_total} 个）")
        for spec, stats in route_stats:
            print(
                f"        - {spec.title}｜状态 {spec.state}｜"
                f"{'新建' if stats['created'] else '已存在'}｜"
                f"{'新冻结版本' if stats['version_created'] else '版本已存在'}"
            )
        print(
            "[完成] 演示账号：editor@demo.local / reviewer@demo.local / demo@demo.local，"
            f"密码 {PASSWORD}"
        )
        print(f"[完成] 已发布路线新增 {published_new} 条（发现页可直接看到）")
        print(f"[完成] 待审投稿新增 {pending_new} 条（审核后台可直接看到）")
        return 0
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[失败] 灌种子出错，已回滚：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
