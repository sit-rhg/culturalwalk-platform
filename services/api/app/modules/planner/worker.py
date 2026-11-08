# 行程规划 · 后台 worker（PL-05）
"""轮询 plan_jobs，把排队的生成任务跑掉。

用法::

    python -m app.modules.planner.worker --once    # 跑一条就退出（本地验证 / CI 用）
    python -m app.modules.planner.worker           # 常驻轮询

为什么用数据库当队列、不引 Celery/RQ：一次生成十几秒、任务是几十条的量级，
数据库轮询完全扛得住，却少一个要运维的组件（也就少一个半夜会炸的东西）。
真到了扛不住的那天，要改的只有本文件。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.modules.planner.agents.base import PlaceSource
from app.modules.planner.service import PlannerService

logger = logging.getLogger(__name__)

# 没有活时的轮询间隔。2 秒是「用户不觉得慢」和「数据库不觉得被烦」的折中。
POLL_INTERVAL_SECONDS = 2.0


def worker_name() -> str:
    """这个进程的身份，写进 plan_jobs.claimed_by。

    排查「任务卡在谁手里」时，只有这一列能回答"是哪个进程领走的"。
    """
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def claim_and_run(
    db: Session,
    *,
    place_source: PlaceSource | None = None,
    now: datetime | None = None,
) -> bool:
    """领一条任务并执行完；没有活可干时返回 False。

    返回 bool 而不是计数：调用方只关心「这一轮干了活没有」，
    干成没干成看 plan_jobs 和行程的状态，不靠返回值猜。
    """
    service = PlannerService(db)
    job = service.claim_job(worker=worker_name(), now=now)
    if job is None:
        db.commit()
        return False

    # 先把「我领到了」提交掉：接下来是十几秒的模型调用，
    # 不能让它一直锁着这一行，否则别的 worker 只能干等。
    db.commit()
    job_id = job.id
    try:
        service.execute_job(job, place_source=place_source)
    except Exception as exc:  # noqa: BLE001 - 一条任务炸掉不能带走整个 worker
        logger.exception("任务 %s 执行时抛出未处理异常", job_id)
        # 异常可能留下半截写入，先回滚再按 id 重取任务记账；
        # 不回滚的话这条连接上的后续查询全废，worker 会被一条坏任务拖死。
        db.rollback()
        service.record_crashed_job(job_id, error=f"{type(exc).__name__}: {exc}")
    db.commit()
    return True


def run_once(*, place_source: PlaceSource | None = None) -> bool:
    """自建会话跑一条任务，给命令行和测试用。"""
    db = SessionLocal()
    try:
        return claim_and_run(db, place_source=place_source)
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行程生成 worker")
    parser.add_argument("--once", action="store_true", help="只跑一条任务就退出")
    parser.add_argument(
        "--interval",
        type=float,
        default=POLL_INTERVAL_SECONDS,
        help="没有任务时的轮询间隔（秒）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.once:
        ran = run_once()
        logger.info("跑完一条任务" if ran else "当前没有待处理的任务")
        return 0

    logger.info("worker 启动：%s，轮询间隔 %.1fs", worker_name(), args.interval)
    try:
        while True:
            try:
                ran = run_once()
            except Exception:  # noqa: BLE001 - 单轮失败（含数据库抖动）不该让常驻进程退出
                logger.exception("本轮任务执行失败，稍后继续轮询")
                time.sleep(args.interval)
                continue
            if not ran:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("worker 收到中断，退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
