"""演示种子数据：3 个账号 + 长沙 6 个点位 + 10 条路线 + 3 份预生成行程。

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
- 10 条已发布路线（官方精选 5 条 + 用户投稿 5 条）+ 1 条待审草稿，覆盖历史、
  建筑、民俗、美食、自然、市井、书院、夜景、江景、近代史等主题，时长 2–6 小时，
  每条 2–5 个节点，节点只从上面 6 个点位里选（会重复使用，但走法各不相同）。
  注意：发布/审核端点（MD-02/MD-04）尚未实现，因此这里用 RouteDraft +
  冻结的 RouteVersion 表达：APPROVED_LOCKED 视为已发布，SUBMITTED_LOCKED
  视为待审，另外补了 submissions / publications / review_tasks 行，让发现页与
  审核后台有东西可读。等对应模块落地后应改为调用 service。
- 3 份预生成行程（demo@demo.local 名下，Planner 的 READY 终态）：直接按
  itineraries / itinerary_days / itinerary_items 的数据结构落库，并补一条
  plan_runs + 四个 agent_steps，让行程详情页的“多 Agent 留痕”也是满的。
  价格一律留空（confidence=unverified），不替点位编票价。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
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
    # 适合人群（多选）；默认空表示「未标注」，发现页按 audience 筛选时不会命中。
    audience: list[str] = field(default_factory=list)


DEMO_ROUTES: tuple[DemoRoute, ...] = (
    DemoRoute(
        key="demo-seed-route-a-v1",
        audience=["family", "student"],
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
        audience=["student", "solo"],
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
        key="demo-seed-route-c-v1",
        audience=["student", "solo"],
        title="马王堆与潮宗街：一天读长沙",
        summary=(
            "上午在湖南省博物馆看马王堆汉墓的素纱襌衣与 T 形帛画，下午钻进潮宗街的麻石巷，"
            "最后从太平老街收尾——长沙的两千年挤在一天里。"
        ),
        theme_tags=["历史", "民俗", "建筑"],
        duration_minutes=330,
        state="APPROVED_LOCKED",
        channel="official",
        nodes=(
            DemoNode("hunan-museum", 150, None, "马王堆陈列是重点，周一闭馆，记得提前预约。"),
            DemoNode(
                "chaozong-street",
                90,
                "metro",
                "看麻石路、老教堂与时务学堂旧址，街区 2019 年完成有机更新。",
            ),
            DemoNode("taiping-street", 60, "walk", "沿湘江路向南走到太平老街，边走边吃。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-d-v1",
        audience=["couple", "family"],
        title="登天心阁，望湘江北去",
        summary=(
            "从建在古城墙上的天心阁开始，过江到橘子洲头看“湘江北去”，再回太平老街吃一顿晚饭——"
            "一条把长沙地形串起来的路线。"
        ),
        theme_tags=["建筑", "江景", "夜景"],
        duration_minutes=300,
        state="APPROVED_LOCKED",
        channel="official",
        nodes=(
            DemoNode("tianxin-pavilion", 90, None, "登阁看老城屋顶，城墙是全国重点文物保护单位。"),
            DemoNode("orange-isle", 120, "metro", "地铁 1 号线转 4 号线到橘子洲站，走到洲头。"),
            DemoNode("taiping-street", 60, "walk", "过江回老城，入夜烟火气最重。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-e-v1",
        audience=["student", "group"],
        title="从博物院到太平老街：文物里的市井",
        summary=(
            "先看湖南省博物馆的马王堆陈列，再坐地铁到太平老街，把汉墓里的器物和街面上的烟火气对上。"
        ),
        theme_tags=["历史", "市井", "美食"],
        duration_minutes=240,
        state="APPROVED_LOCKED",
        channel="official",
        nodes=(
            DemoNode("hunan-museum", 120, None, "按展厅顺序走：马王堆、青铜、陶瓷。"),
            DemoNode("taiping-street", 90, "metro", "地铁 2 号线到五一广场，步行进太平老街。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-f-v1",
        audience=["couple", "solo"],
        title="湘江夜：橘子洲头与太平街的灯",
        summary="傍晚进橘子洲走到洲头看日落，过江后在太平老街吃夜宵——天黑了才开始的半条路线。",
        theme_tags=["夜景", "江景", "美食"],
        duration_minutes=180,
        state="APPROVED_LOCKED",
        channel="official",
        nodes=(
            DemoNode("orange-isle", 90, None, "傍晚进洲，洲头等日落与江灯。"),
            DemoNode("taiping-street", 75, "walk", "过江到太平老街，吃夜宵收尾。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-g-v1",
        audience=["couple", "group"],
        title="太平街到潮宗街：两条老街的午后",
        summary="在太平老街吃一顿，再沿湘江路走到潮宗街，看麻石路、老教堂和巷子里的新店。",
        theme_tags=["美食", "市井", "民俗"],
        duration_minutes=180,
        state="APPROVED_LOCKED",
        channel="user",
        nodes=(
            DemoNode("taiping-street", 100, None, "从五一大道口进街，边走边吃。"),
            DemoNode("chaozong-street", 60, "walk", "沿湘江路向北，走进潮宗街的麻石巷。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-h-v1",
        audience=["solo", "couple"],
        title="潮宗街的旧墙与新店",
        summary="两小时走完潮宗街：老教堂、时务学堂旧址在巷子深处，新开的咖啡馆就贴着旧墙。",
        theme_tags=["民俗", "建筑", "市井"],
        duration_minutes=120,
        state="APPROVED_LOCKED",
        channel="user",
        nodes=(
            DemoNode("chaozong-street", 75, None, "从老教堂走到时务学堂旧址，再沿麻石路折回。"),
            DemoNode("taiping-street", 45, "walk", "往南走到太平老街，顺路吃碗粉。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-i-v1",
        audience=["family", "group"],
        title="岳麓山脚的一整天：书院、江洲与日落",
        summary="上午把岳麓书院走完，下午过江去橘子洲，在洲头等日落——一整天都在岳麓区的地界上转。",
        theme_tags=["书院", "自然", "江景"],
        duration_minutes=360,
        state="APPROVED_LOCKED",
        channel="user",
        nodes=(
            DemoNode("yuelu-academy", 150, None, "讲堂、御书楼、半学斋按中轴走一遍。"),
            DemoNode("orange-isle", 150, "metro", "地铁 4 号线过江，在洲上等日落。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-j-v1",
        audience=["student", "senior"],
        title="城墙上的近代史：天心阁与潮宗街",
        summary=(
            "从天心阁的古城墙走到潮宗街的时务学堂旧址，再拐进太平老街——长沙的近代化就在这几条巷子里。"
        ),
        theme_tags=["近代史", "建筑", "历史"],
        duration_minutes=240,
        state="APPROVED_LOCKED",
        channel="user",
        nodes=(
            DemoNode("tianxin-pavilion", 90, None, "先看长沙仅存的一段古城墙。"),
            DemoNode(
                "chaozong-street",
                90,
                "metro",
                "时务学堂旧址在巷子深处，维新变法曾从这里起步。",
            ),
            DemoNode("taiping-street", 45, "walk", "最后一站回到街面上，贾谊故居也在这一带。"),
        ),
    ),
    DemoRoute(
        key="demo-seed-route-pending-v1",
        audience=["senior", "group"],
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


@dataclass(frozen=True, slots=True)
class DemoPlanItem:
    slug: str
    start: str  # "09:00"，入库时转 time；只用于演示，不接排班算法
    stay_minutes: int
    transport: str | None


@dataclass(frozen=True, slots=True)
class DemoPlanDay:
    summary: str
    items: tuple[DemoPlanItem, ...]


@dataclass(frozen=True, slots=True)
class DemoPlan:
    key: str
    days: int
    people: int
    budget_total: int  # 分
    preferences: dict[str, object]
    plan_days: tuple[DemoPlanDay, ...]


# 3 份预生成行程：直接写成 READY 终态，答辩时打开“我的行程”就有内容可点。
# 主题分别覆盖历史建筑、美食市井、自然江景，日期留空（演示不绑定具体日子）。
DEMO_PLANS: tuple[DemoPlan, ...] = (
    DemoPlan(
        key="demo-seed-plan-history-2d",
        days=2,
        people=2,
        budget_total=80000,
        preferences={
            "include_lodging": False,
            "day_parts": ["morning", "afternoon"],
            "themes": ["历史", "建筑"],
            "transport": ["metro", "walk"],
            "pace": "normal",
            "start_point": None,
        },
        plan_days=(
            DemoPlanDay(
                summary="岳麓山下：书院与江洲",
                items=(
                    DemoPlanItem("yuelu-academy", "09:00", 120, None),
                    DemoPlanItem("orange-isle", "14:00", 120, "metro"),
                ),
            ),
            DemoPlanDay(
                summary="老城一天：博物院、潮宗街、太平街",
                items=(
                    DemoPlanItem("hunan-museum", "09:30", 150, "metro"),
                    DemoPlanItem("chaozong-street", "14:30", 75, "metro"),
                    DemoPlanItem("taiping-street", "16:30", 60, "walk"),
                ),
            ),
        ),
    ),
    DemoPlan(
        key="demo-seed-plan-streetfood-1d",
        days=1,
        people=3,
        budget_total=40000,
        preferences={
            "include_lodging": False,
            "day_parts": ["afternoon", "evening"],
            "themes": ["美食", "市井"],
            "transport": ["walk", "metro"],
            "pace": "relaxed",
            "start_point": None,
        },
        plan_days=(
            DemoPlanDay(
                summary="从太平老街吃到潮宗街",
                items=(
                    DemoPlanItem("taiping-street", "11:00", 100, None),
                    DemoPlanItem("tianxin-pavilion", "14:00", 60, "walk"),
                    DemoPlanItem("chaozong-street", "16:30", 75, "metro"),
                ),
            ),
        ),
    ),
    DemoPlan(
        key="demo-seed-plan-river-night-2d",
        days=2,
        people=2,
        budget_total=60000,
        preferences={
            "include_lodging": False,
            "day_parts": ["afternoon", "evening"],
            "themes": ["自然", "江景", "夜景"],
            "transport": ["metro", "walk"],
            "pace": "normal",
            "start_point": None,
        },
        plan_days=(
            DemoPlanDay(
                summary="岳麓山脚到江心洲",
                items=(
                    DemoPlanItem("yuelu-academy", "13:30", 90, None),
                    DemoPlanItem("orange-isle", "16:00", 120, "metro"),
                ),
            ),
            DemoPlanDay(
                summary="博物院与老街的夜",
                items=(
                    DemoPlanItem("hunan-museum", "10:00", 120, "metro"),
                    DemoPlanItem("taiping-street", "17:00", 90, "metro"),
                ),
            ),
        ),
    ),
)


# 四个 Agent 的留痕顺序与 planner 的 Orchestrator 一致（researcher → planner →
# critic → verifier），step_index 从 0 开始，和真实流水线写出来的行同形。
def _demo_agent_steps() -> tuple[tuple[AgentName, int], ...]:
    """四个 Agent 的留痕顺序与真实流水线一致；做成函数是因为 app 的 import 在文件末段。"""
    return (
        (AgentName.RESEARCHER, 2400),
        (AgentName.PLANNER, 5200),
        (AgentName.CRITIC, 3100),
        (AgentName.VERIFIER, 1800),
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
from app.modules.planner.models import (  # noqa: E402
    AgentName,
    AgentStep,
    AgentStepStatus,
    Itinerary,
    ItineraryDay,
    ItineraryItem,
    PlanRun,
    PlanState,
)
from app.modules.planner.pricing import REASON_UNKNOWN, UNVERIFIED  # noqa: E402
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
            audience=list(spec.audience),
        ),
        idempotency_key=spec.key,
    )
    # 来源通道不进契约（RouteDraftCreate 没有这个字段），演示数据自己落：
    # 必须在 freeze_version 之前设好，否则快照里没有 channel，发布后会被当成用户投稿。
    if draft.channel != spec.channel:
        draft.channel = spec.channel
        session.flush()

    # 同 key 重放时 replace_nodes 会在写库前直接返回，所以这里第二遍也是安全的。
    if list(draft.audience or []) != spec.audience:
        draft.audience = list(spec.audience)
        session.flush()
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


def _plan_id(key: str) -> uuid.UUID:
    """行程主键按 key 派生：同一份演示行程永远同一个 UUID，重跑才认得出“已经灌过”。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"culturalwalk:demo:itinerary:{key}")


def _plan_row_id(key: str, *parts: object) -> uuid.UUID:
    """逐日安排 / 生成记录 / 步骤的主键同样派生，保证一行不会被写两遍。"""
    tail = ":".join(str(part) for part in parts)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"culturalwalk:demo:itinerary:{key}:{tail}")


def _plan_start_time(text: str) -> time:
    hour, minute = text.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _plan_minutes(text: str) -> int:
    start = _plan_start_time(text)
    return start.hour * 60 + start.minute


def _check_plan_spec(spec: DemoPlan) -> None:
    """写库前先校验手写日程：天数对不上、时间倒流、排过零点都直接报错。

    这些错误在页面上只表现为“顺序怪怪的”，不报错就很难在答辩前发现。
    """
    if len(spec.plan_days) != spec.days:
        raise RuntimeError(f"行程 {spec.key} 写了 {len(spec.plan_days)} 天，但 days={spec.days}")
    for day_index, day in enumerate(spec.plan_days, start=1):
        if not day.items:
            raise RuntimeError(f"行程 {spec.key} 第 {day_index} 天没有任何安排")
        cursor = -1
        for item in day.items:
            start = _plan_minutes(item.start)
            if start < cursor:
                raise RuntimeError(f"行程 {spec.key} 第 {day_index} 天的 {item.slug} 与上一站重叠")
            cursor = start + item.stay_minutes
            if cursor > 24 * 60:
                raise RuntimeError(f"行程 {spec.key} 第 {day_index} 天排过了 24:00")
            if item.slug not in DEMO_SLUGS:
                raise RuntimeError(f"行程 {spec.key} 用到了演示点位之外的 slug：{item.slug}")


def _ensure_plan(session, user: User, spec: DemoPlan) -> dict[str, object]:
    """按 Planner 的表结构直接落一份 READY 行程；已存在就整条跳过，不覆盖。

    不经过 PlannerService：它按 Idempotency-Key + input_hash 建 DRAFT 再排队给
    worker，这里要的是“库里已经有一份最终态行程”，直接写表最接近演示现场。
    """
    existing = session.get(Itinerary, _plan_id(spec.key))
    if existing is not None:
        return {"created": False, "itinerary": existing}

    _check_plan_spec(spec)

    input_hash = hashlib.sha256(
        json.dumps(
            {
                "city": "长沙",
                "days": spec.days,
                "people": spec.people,
                "budget_total": spec.budget_total,
                "preferences": spec.preferences,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    itinerary = Itinerary(
        id=_plan_id(spec.key),
        user_id=user.id,
        city="长沙",
        days=spec.days,
        people=spec.people,
        budget_total=spec.budget_total,
        preferences=spec.preferences,
        state=PlanState.READY,
        idempotency_key=f"demo-seed:{spec.key}",
        input_hash=input_hash,
    )
    session.add(itinerary)
    session.flush()

    for day_index, day in enumerate(spec.plan_days, start=1):
        day_row = ItineraryDay(
            id=_plan_row_id(spec.key, "day", day_index),
            itinerary_id=itinerary.id,
            day_index=day_index,
            summary=day.summary,
        )
        session.add(day_row)
        session.flush()
        for order, item in enumerate(day.items, start=1):
            session.add(
                ItineraryItem(
                    id=_plan_row_id(spec.key, "day", day_index, "item", order),
                    day_id=day_row.id,
                    order=order,
                    place_id=place_id(item.slug),
                    start_time=_plan_start_time(item.start),
                    stay_minutes=item.stay_minutes,
                    transport=item.transport,
                    # 演示点位没有票价来源：留空 + unverified，前端才会标“待核实”。
                    # 填 0 会假装免费，填数字则是在编来源，两种都比留空更糟。
                    price_min=None,
                    price_max=None,
                    confidence=UNVERIFIED,
                    source_ids=[],
                    note=REASON_UNKNOWN,
                )
            )

    run = PlanRun(
        id=_plan_row_id(spec.key, "run"),
        itinerary_id=itinerary.id,
        state=PlanState.READY,
        input_hash=input_hash,
        prompt_version="demo-seed",
    )
    session.add(run)
    session.flush()

    for index, (agent, duration_ms) in enumerate(_demo_agent_steps()):
        session.add(
            AgentStep(
                id=_plan_row_id(spec.key, "step", index),
                run_id=run.id,
                agent=agent,
                status=AgentStepStatus.DONE,
                step_index=index,
                output_digest=hashlib.sha256(f"{spec.key}:{agent}".encode()).hexdigest(),
                duration_ms=duration_ms,
            )
        )
    session.flush()
    return {"created": True, "itinerary": itinerary}


def _plan_item_count(session, itinerary_id: uuid.UUID) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(ItineraryItem)
        .join(ItineraryDay, ItineraryItem.day_id == ItineraryDay.id)
        .where(ItineraryDay.itinerary_id == itinerary_id)
    )
    return int(count or 0)


def _print_plans(session, plan_stats: list[tuple[DemoPlan, dict[str, object]]]) -> None:
    print(f"[种子] 行程：{len(DEMO_PLANS)} 条预生成（demo@demo.local，状态 READY）")
    for spec, stats in plan_stats:
        themes = spec.preferences.get("themes")
        theme_text = "、".join(str(tag) for tag in themes) if isinstance(themes, list) else ""
        print(
            f"        - {spec.days} 天 / {spec.people} 人 / 预算 {spec.budget_total // 100} 元"
            f"｜{theme_text}｜{_plan_item_count(session, stats['itinerary'].id)} 项安排"
            f"｜{'新建' if stats['created'] else '已存在'}"
        )


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

        plan_stats = []
        plans_new = 0
        for spec in DEMO_PLANS:
            stats = _ensure_plan(session, users["user"], spec)
            plans_new += int(stats["created"])
            plan_stats.append((spec, stats))

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
        official_total = sum(1 for spec in DEMO_ROUTES if spec.channel == "official")
        user_total = sum(1 for spec in DEMO_ROUTES if spec.channel == "user")
        print(
            f"[种子] 路线：{len(DEMO_ROUTES)} 条（官方 {official_total} / 投稿 {user_total}，"
            f"节点 {node_total} 个）"
        )
        for spec, stats in route_stats:
            print(
                f"        - {spec.title}｜{'官方' if spec.channel == 'official' else '投稿'}｜"
                f"状态 {spec.state}｜{'新建' if stats['created'] else '已存在'}｜"
                f"{'新冻结版本' if stats['version_created'] else '版本已存在'}"
            )
        _print_plans(session, plan_stats)
        print(
            "[完成] 演示账号：editor@demo.local / reviewer@demo.local / demo@demo.local，"
            f"密码 {PASSWORD}"
        )
        published_total = session.scalar(
            select(func.count())
            .select_from(Publication)
            .where(Publication.state == PublicationState.PUBLISHED)
        )
        ready_plans = session.scalar(
            select(func.count())
            .select_from(Itinerary)
            .where(
                Itinerary.user_id == users["user"].id,
                Itinerary.state == PlanState.READY,
            )
        )
        print(
            f"[完成] 已发布路线新增 {published_new} 条，库里 PUBLISHED 共 {published_total} 条"
            "（发现页可直接看到）"
        )
        print(f"[完成] 待审投稿新增 {pending_new} 条（审核后台可直接看到）")
        print(
            f"[完成] 预生成行程新增 {plans_new} 条，demo@demo.local 名下 READY 共 {ready_plans} 条"
        )
        print(
            f"[验收] M0-07 已发布路线 {published_total} 条（要求 ≥10）｜"
            f"QA-03 预生成行程 {ready_plans} 条（要求 ≥3）"
        )
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
