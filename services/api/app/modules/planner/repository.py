# 行程规划（多 Agent） · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.modules.planner.models import (
    AgentStep,
    Itinerary,
    ItineraryDay,
    ItineraryItem,
    JobState,
    PlanJob,
    PlanRun,
)


class PlannerRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> Itinerary | None:
        """按 (用户, 幂等键) 找已有的行程。

        带 deleted_at 过滤：用户删掉行程后重新提交同一个 key，应该当新请求处理，
        而不是把一张已删除的行程还回去。
        """
        stmt = select(Itinerary).where(
            Itinerary.user_id == user_id,
            Itinerary.idempotency_key == idempotency_key,
            Itinerary.deleted_at.is_(None),
        )
        return self.db.scalars(stmt).first()

    def add(self, itinerary: Itinerary) -> Itinerary:
        self.db.add(itinerary)
        return itinerary

    def get_itinerary(self, itinerary_id: uuid.UUID) -> Itinerary | None:
        stmt = select(Itinerary).where(Itinerary.id == itinerary_id, Itinerary.deleted_at.is_(None))
        return self.db.scalars(stmt).first()

    def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Itinerary]:
        """按 (created_at, id) 倒序取一页行程，after 是上一页最后一条的位置。

        已软删的行程一律不出现：deleted_at 只留给后台任务和幂等判断用，
        用户不该在列表里再见到它。
        """
        stmt = select(Itinerary).where(
            Itinerary.user_id == user_id,
            Itinerary.deleted_at.is_(None),
        )
        if after is not None:
            created_at, itinerary_id = after
            # 不走行值比较：SQLite 与 PostgreSQL 对复合比较的支持不一致
            stmt = stmt.where(
                or_(
                    Itinerary.created_at < created_at,
                    and_(Itinerary.created_at == created_at, Itinerary.id < itinerary_id),
                )
            )
        stmt = stmt.order_by(Itinerary.created_at.desc(), Itinerary.id.desc()).limit(limit)
        return list(self.db.scalars(stmt))

    def latest_runs(self, itinerary_ids: list[uuid.UUID]) -> dict[uuid.UUID, PlanRun]:
        """批量取每张行程最近一次生成记录，key 是行程 id。

        列表页每条都要带 run_id；逐条查会变成 N+1，一次 IN 查询按时间倒序
        扫一遍，每张行程第一次出现的 run 就是最新的。
        """
        if not itinerary_ids:
            return {}
        stmt = (
            select(PlanRun)
            .where(PlanRun.itinerary_id.in_(itinerary_ids))
            .order_by(PlanRun.created_at.desc(), PlanRun.id.desc())
        )
        result: dict[uuid.UUID, PlanRun] = {}
        for run in self.db.scalars(stmt):
            result.setdefault(run.itinerary_id, run)
        return result

    def soft_delete(self, itinerary: Itinerary, *, when: datetime) -> None:
        """软删除：只打时间戳，内容留给后台任务判断"行程已不存在"。

        不物理删：plan_runs 上有 ON DELETE CASCADE 的关联数据，
        删了会把"生成过几次、为什么失败"一并抹掉，审计也就无从对账。
        """
        itinerary.deleted_at = when
        self.db.flush()

    def latest_run(self, itinerary_id: uuid.UUID) -> PlanRun | None:
        """最近一次生成记录。

        一张行程可能被重新生成多次，详情页关心的是"这一次"，
        所以按时间倒序取第一条，而不是把所有 run 都返回。
        """
        stmt = (
            select(PlanRun)
            .where(PlanRun.itinerary_id == itinerary_id)
            .order_by(PlanRun.created_at.desc())
            .limit(1)
        )
        return self.db.scalars(stmt).first()

    def steps_for_run(self, run_id: uuid.UUID) -> list[AgentStep]:
        """按执行顺序取某个 run 的步骤留痕。

        按 step_index 排，不按 id：id 是随机 UUID，按它排出来的顺序是随机的。
        """
        stmt = (
            select(AgentStep)
            .where(AgentStep.run_id == run_id)
            .order_by(AgentStep.step_index, AgentStep.created_at)
        )
        return list(self.db.scalars(stmt).all())

    # ----- 行程内容（PL-06） -------------------------------------------------

    def days_for_itinerary(self, itinerary_id: uuid.UUID) -> list[ItineraryDay]:
        """取一份行程的逐日安排，连带把当天每一项一起捞出来。

        `selectinload` 而不是惰性加载：详情接口要一次性把 days + items 都序列化，
        惰性加载会变成 N+1 次查询；一次 IN 查询把 items 全捞回来更省。
        """
        stmt = (
            select(ItineraryDay)
            .where(ItineraryDay.itinerary_id == itinerary_id)
            .order_by(ItineraryDay.day_index)
            .options(selectinload(ItineraryDay.items))
        )
        return list(self.db.scalars(stmt).all())

    def clear_plan_content(self, itinerary_id: uuid.UUID) -> None:
        """清掉一份行程已有的逐日安排。

        重新生成时先清后写：否则旧的第 3 天会留在新生成的两天行程里，
        用户看到的是两次结果的混合体。先删 items 再删 days，避免外键悬挂。
        """
        day_ids = select(ItineraryDay.id).where(ItineraryDay.itinerary_id == itinerary_id)
        self.db.execute(delete(ItineraryItem).where(ItineraryItem.day_id.in_(day_ids)))
        self.db.execute(delete(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id))
        self.db.flush()

    # ----- 后台任务（PL-05） -------------------------------------------------

    def add_job(self, job: PlanJob) -> PlanJob:
        self.db.add(job)
        return job

    def get_job_by_run(self, run_id: uuid.UUID) -> PlanJob | None:
        stmt = select(PlanJob).where(PlanJob.run_id == run_id)
        return self.db.scalars(stmt).first()

    def claim_next_job(
        self,
        *,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
        worker: str,
    ) -> PlanJob | None:
        """领一条最该干的任务，领不到就返回 None。

        `FOR UPDATE SKIP LOCKED`：两个 worker 同时轮询时，一个锁住这行，
        另一个直接跳过、去拿下一条，而不是排队等——排队会让多开 worker 变成
        负优化。SQLite 不支持行锁，方言会忽略这段，单进程测试仍然正确。
        """
        stmt = (
            select(PlanJob)
            .where(
                PlanJob.state == JobState.PENDING,
                PlanJob.available_at <= now,
                PlanJob.attempts < max_attempts,
            )
            .order_by(PlanJob.available_at, PlanJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = self.db.scalars(stmt).first()
        if job is None:
            return None

        job.state = JobState.RUNNING
        job.attempts += 1
        job.claimed_at = now
        job.claimed_by = worker
        job.lease_until = now + timedelta(seconds=lease_seconds)
        return job

    def recover_expired_leases(self, *, now: datetime) -> int:
        """把租约过期的 RUNNING 任务放回队列（worker 崩了没人收尸）。

        只改状态不改 attempts：崩掉那次的尝试次数已经算过，这里不重复扣。
        """
        stmt = (
            update(PlanJob)
            .where(
                PlanJob.state == JobState.RUNNING,
                PlanJob.lease_until.is_not(None),
                PlanJob.lease_until < now,
            )
            .values(
                state=JobState.PENDING,
                claimed_at=None,
                claimed_by=None,
                lease_until=None,
                available_at=now,
            )
        )
        result = self.db.execute(stmt)
        return int(result.rowcount or 0)
