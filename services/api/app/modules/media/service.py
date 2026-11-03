# 媒体资源 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：MediaAsset
明确不做：内容审核

生命周期（docs/02 §2.4）::

    quarantine(pending) --complete--> scanning --扫描通过--> clean --publish--> published

对外契约里的 MediaAsset 只有 zone / scan_state 两个状态字段：
- scan_state=pending    还没确认上传（上传地址可能已过期）
- scan_state=scanning   已确认上传，等待扫描（zone 仍是 quarantine）
- scan_state=clean      扫描通过（zone 变 clean，此时才允许给出公开 URL / 被引用）
- scan_state=rejected   扫描拒绝（终态，只能重新申请上传）
- published             用 published_at 标记，不额外加契约外字段
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized
from app.modules.audit import service as audit
from app.modules.media.adapters.objects import ObjectNotFound, ObjectStore
from app.modules.media.adapters.storage import (
    StorageSigner,
    default_upload_expiry,
    get_storage_signer,
)
from app.modules.media.models import MediaAsset, MediaZone, ScanState, utcnow
from app.modules.media.repository import MediaRepository
from app.modules.media.scan import ScanRejected, scan_image, suffix_for
from app.modules.media.schemas import MediaAssetOut, MediaIntentIn, MediaIntentOut

# 能看别人媒体的角色（编辑端排障）；普通用户只能看自己上传的。
_REVIEWER_ROLES = frozenset({"admin", "editor"})

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _actor_id(actor: CurrentUser) -> uuid.UUID:
    """token 的 sub 不是 UUID 说明令牌已失效，按未登录处理（与 notifications 一致）。"""
    try:
        return uuid.UUID(actor.id)
    except ValueError as exc:
        raise Unauthorized("登录已失效，请重新登录") from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _safe_object_name(filename: str) -> str:
    """对象 key 里只留安全字符；文件名已经过 schema 的 basename 清洗。"""
    cleaned = _UNSAFE_NAME.sub("_", filename).strip("._") or "upload"
    return cleaned[:120]


class MediaService:
    """依赖注入的官方入口；外部模块只能通过它访问本模块能力。"""

    def __init__(self, db: Session, signer: StorageSigner | None = None) -> None:
        self.db = db
        self.repo = MediaRepository(db)
        self.signer = signer or get_storage_signer()

    # ---------- 写 ----------

    def create_intent(
        self,
        *,
        actor: CurrentUser,
        payload: MediaIntentIn,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> MediaIntentOut:
        """申请上传：落一条 quarantine/pending 资产并返回预签名地址。

        幂等：同一上传者重放同一个 Idempotency-Key 不会产生第二条资产，
        返回首次那条（上传地址与过期时间都保持不变，客户端可继续用）。
        """
        owner_id = _actor_id(actor)
        existing = self.repo.get_by_idempotency_key(
            owner_id=owner_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return self._intent_out(existing)

        asset_id = uuid.uuid4()
        asset = MediaAsset(
            id=asset_id,
            owner_id=owner_id,
            # 隔离区路径由服务端决定：客户端不能指定 key，否则可覆盖别人的对象。
            object_key=f"quarantine/{owner_id}/{asset_id}/{_safe_object_name(payload.filename)}",
            filename=payload.filename,
            mime=payload.mime,
            size=payload.size,
            zone=MediaZone.QUARANTINE,
            scan_state=ScanState.PENDING,
            upload_expires_at=default_upload_expiry(),
            idempotency_key=idempotency_key,
        )
        self.repo.add(asset)
        audit.write(
            self.db,
            action="media.intent.create",
            object_type="media_asset",
            object_id=str(asset.id),
            actor_id=str(owner_id),
            actor_username=actor.username,
            after_hash=audit.AuditService.digest(
                {"filename": payload.filename, "mime": payload.mime, "size": payload.size}
            ),
            request_id=request_id,
        )
        return self._intent_out(asset)

    def complete(
        self,
        *,
        media_id: uuid.UUID,
        actor: CurrentUser,
        request_id: str | None = None,
    ) -> MediaAssetOut:
        """确认上传完成：pending -> scanning（入队扫描）。

        重复调用是幂等的：已经在扫描/已 clean/已发布时原样返回 202，不重复写审计。
        过期或已拒绝则 409 —— 这两种情况下文件要么可能没传成，要么已被判不合格，
        继续假装"扫描中"会让前端无限等待。
        """
        asset = self.repo.get(media_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.owner_id != _actor_id(actor):
            raise Forbidden("只能确认自己上传的文件", code="MEDIA_FORBIDDEN")

        if asset.scan_state is ScanState.REJECTED:
            raise Conflict("媒体未通过扫描，请重新上传", code="MEDIA_REJECTED")
        if asset.scan_state is ScanState.PENDING:
            now = utcnow()
            if _as_utc(asset.upload_expires_at) <= now:
                raise Conflict("上传地址已过期，请重新申请", code="MEDIA_UPLOAD_EXPIRED")
            asset.scan_state = ScanState.SCANNING
            asset.completed_at = now
            audit.write(
                self.db,
                action="media.complete",
                object_type="media_asset",
                object_id=str(asset.id),
                actor_id=str(asset.owner_id),
                actor_username=actor.username,
                after_hash=audit.AuditService.digest({"scan_state": ScanState.SCANNING.value}),
                request_id=request_id,
            )
        return self._asset_out(asset)

    # ---------- 读 ----------

    def get_for_viewer(self, *, media_id: uuid.UUID, actor: CurrentUser) -> MediaAssetOut:
        """上传者本人或编辑可查；找不到与无权查看分开报，便于前端提示。"""
        asset = self.repo.get(media_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.owner_id != _actor_id(actor) and actor.role not in _REVIEWER_ROLES:
            raise Forbidden("只能查看自己上传的文件", code="MEDIA_FORBIDDEN")
        return self._asset_out(asset)

    def require_referenceable(self, media_id: uuid.UUID) -> MediaAsset:
        """给其他模块用的引用闸门：clean 之前不允许被路线/地点引用或发布。

        返回 ORM 对象（同模块事务内使用），调用方不必再查一次。
        """
        asset = self.repo.get(media_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.zone is not MediaZone.CLEAN or asset.scan_state is not ScanState.CLEAN:
            raise Conflict("媒体尚未通过扫描，不能引用或发布", code="MEDIA_NOT_CLEAN")
        return asset

    # ---------- 扫描/发布钩子（当前无公开端点） ----------

    def mark_clean(self, media_id: uuid.UUID, *, request_id: str | None = None) -> MediaAssetOut:
        """扫描器回调：quarantine/scanning -> clean。

        真实扫描服务接入后由它调用；现在供测试与后续扫描 worker 使用。
        """
        asset = self.repo.get(media_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.scan_state is ScanState.REJECTED:
            raise Conflict("已拒绝的媒体不能改为通过", code="MEDIA_REJECTED")
        if asset.scan_state is ScanState.CLEAN:
            return self._asset_out(asset)
        asset.scan_state = ScanState.CLEAN
        asset.zone = MediaZone.CLEAN
        asset.url = self._public_url(asset)
        audit.write(
            self.db,
            action="media.scan.clean",
            object_type="media_asset",
            object_id=str(asset.id),
            after_hash=audit.AuditService.digest({"scan_state": ScanState.CLEAN.value}),
            request_id=request_id,
        )
        return self._asset_out(asset)

    def mark_rejected(
        self,
        media_id: uuid.UUID,
        *,
        code: str,
        detail: str,
        request_id: str | None = None,
    ) -> MediaAssetOut:
        """扫描不通过：scanning -> rejected（终态，只能重新申请上传）。

        重复标记是幂等的；已 clean 的资产不允许再标 rejected —— 那多半是扫描器的
        bug，静默改状态会让已经发布的图片突然消失。
        """
        asset = self.repo.get(media_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.scan_state is ScanState.CLEAN:
            raise Conflict("已通过扫描的媒体不能改为拒绝", code="MEDIA_ALREADY_CLEAN")
        if asset.scan_state is ScanState.REJECTED:
            return self._asset_out(asset)
        asset.scan_state = ScanState.REJECTED
        audit.write(
            self.db,
            action="media.scan.rejected",
            object_type="media_asset",
            object_id=str(asset.id),
            after_hash=audit.AuditService.digest(
                {"scan_state": ScanState.REJECTED.value, "code": code, "detail": detail}
            ),
            request_id=request_id,
        )
        return self._asset_out(asset)

    def scan_asset(
        self,
        media_id: uuid.UUID,
        *,
        store: ObjectStore,
        request_id: str | None = None,
    ) -> MediaAssetOut:
        """扫描一个资产：读原件 → 校验/重编码 → 写 clean 副本与缩略图 → mark_clean()。

        为什么要换 key：quarantine 与 clean 必须是两个前缀（开发范式 §11.1.1），
        对外 URL 只能指向 clean 里的对象；重编码后的字节才是被发布的那一份。
        """
        asset = self.repo.get(media_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.scan_state in (ScanState.CLEAN, ScanState.REJECTED):
            return self._asset_out(asset)
        if asset.scan_state is not ScanState.SCANNING:
            raise Conflict("文件尚未确认上传", code="MEDIA_NOT_UPLOADED")

        try:
            raw = store.get(asset.object_key)
        except ObjectNotFound as exc:
            raise Conflict(
                "对象存储里找不到该文件，请重新上传", code="MEDIA_OBJECT_MISSING"
            ) from exc

        try:
            outcome = scan_image(raw, asset.mime)
        except ScanRejected as rejected:
            return self.mark_rejected(
                asset.id, code=rejected.code, detail=rejected.detail, request_id=request_id
            )

        name = asset.object_key.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "image"
        clean_key = f"clean/{asset.owner_id}/{asset.id}/{name}.{suffix_for(outcome.mime)}"
        store.put(clean_key, outcome.data)
        store.put(f"{clean_key}.thumb.webp", outcome.thumbnail)
        store.delete(asset.object_key)

        asset.object_key = clean_key
        asset.mime = outcome.mime
        asset.size = len(outcome.data)
        asset.checksum = hashlib.sha256(outcome.data).hexdigest()
        asset.width = outcome.width
        asset.height = outcome.height
        # 复用 #110 的扫描通过钩子：zone/scan_state/url/审计都在它里面。
        return self.mark_clean(asset.id, request_id=request_id)

    def scan_pending(
        self, *, store: ObjectStore, limit: int = 10, request_id: str | None = None
    ) -> dict[str, int]:
        """扫一批待扫描资产；单个失败只计数、不打断整批（worker 要能持续推进）。"""
        counts = {"scanned": 0, "clean": 0, "rejected": 0, "skipped": 0}
        for asset in self.repo.list_scanning(limit):
            counts["scanned"] += 1
            try:
                out = self.scan_asset(asset.id, store=store, request_id=request_id)
            except AppError:
                counts["skipped"] += 1
                continue
            if out.scan_state == ScanState.CLEAN.value:
                counts["clean"] += 1
            elif out.scan_state == ScanState.REJECTED.value:
                counts["rejected"] += 1
        return counts

    def publish(
        self,
        media_id: uuid.UUID,
        *,
        actor: CurrentUser | None = None,
        request_id: str | None = None,
    ) -> MediaAssetOut:
        """clean -> published。重复发布不报错，也不重复写审计。"""
        asset = self.require_referenceable(media_id)
        if asset.published_at is None:
            asset.published_at = utcnow()
            audit.write(
                self.db,
                action="media.publish",
                object_type="media_asset",
                object_id=str(asset.id),
                actor_id=actor.id if actor else None,
                actor_username=actor.username if actor else None,
                after_hash=audit.AuditService.digest({"published_at": asset.published_at}),
                request_id=request_id,
            )
        return self._asset_out(asset)

    # ---------- 出参 ----------

    def _intent_out(self, asset: MediaAsset) -> MediaIntentOut:
        ticket = self.signer.presign_upload(
            object_key=asset.object_key,
            mime=asset.mime,
            size=asset.size,
            expires_at=_as_utc(asset.upload_expires_at),
        )
        return MediaIntentOut(
            asset_id=asset.id,
            upload_url=ticket.upload_url,
            fields=dict(ticket.fields),
            expires_at=ticket.expires_at,
        )

    def _asset_out(self, asset: MediaAsset) -> MediaAssetOut:
        clean = asset.zone is MediaZone.CLEAN and asset.scan_state is ScanState.CLEAN
        return MediaAssetOut(
            id=asset.id,
            zone=asset.zone.value,
            scan_state=asset.scan_state.value,
            # 契约要求：clean 之前 url 必须是 null，不能把隔离区地址泄给前端。
            url=asset.url or (self._public_url(asset) if clean else None),
            width=asset.width,
            height=asset.height,
        )

    def _public_url(self, asset: MediaAsset) -> str:
        endpoint = settings.s3_endpoint.rstrip("/")
        return f"{endpoint}/{settings.s3_bucket}/{asset.object_key}"
