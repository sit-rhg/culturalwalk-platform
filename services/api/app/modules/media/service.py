# 媒体资源 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：MediaAsset
明确不做：内容审核

安全边界（docs/…开发范式.md §11.1.1）：
- 签名 URL 只绑一个 object key，且绑死类型与长度上限，签名在 `LocalStorageAdapter` 里校验；
- 隔离区与清洁区用 key 前缀分开，未扫描通过的资产不产出可读 URL；
- 确认时按真实字节的大小与魔数复核，任何一项不符直接把资产标记为 REJECTED，
  不允许"先放过、后面再说"。
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.storage import StorageAdapter
from app.core.config import settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.modules.media.models import MediaAsset, MediaZone, ScanState
from app.modules.media.repository import MediaRepository
from app.modules.media.scan import ScanRejected, scan_image
from app.modules.media.schemas import MediaAssetOut, MediaIntentIn, MediaIntentOut

_ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})
_REUSE_DETAIL = [{"field": "Idempotency-Key", "reason": "already_used_with_different_body"}]


def _input_hash(payload: MediaIntentIn) -> str:
    """幂等重放时比对：同 key 不同内容必须 409，不能悄悄返回上一次的结果。"""
    raw = json.dumps(
        payload.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sniff_mime(data: bytes) -> str | None:
    """只看魔数，不信文件名与 Content-Type。

    只识别契约允许的三种；返回 None 表示"认不出来"，调用方按不一致处理。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class MediaService:
    def __init__(self, db: Session, storage: StorageAdapter) -> None:
        self.db = db
        self.storage = storage
        self.repo = MediaRepository(db)

    # ---- 申请上传 ----

    def create_intent(
        self, *, owner_id: uuid.UUID, payload: MediaIntentIn, idempotency_key: str
    ) -> MediaIntentOut:
        if payload.mime not in _ALLOWED_MIME:  # 理论上被 Literal 拦住，兜底防契约漂移
            raise ValidationFailed(
                "不支持的文件类型",
                details=[{"field": "mime", "reason": "只支持 jpeg/png/webp"}],
            )
        if payload.size > settings.media_max_bytes:
            raise ValidationFailed(
                "文件超过大小上限",
                details=[{"field": "size", "reason": f"最大 {settings.media_max_bytes} 字节"}],
            )

        digest = _input_hash(payload)
        existing = self.repo.get_by_owner_and_key(
            owner_id=owner_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return self._replay(existing, digest)

        asset = MediaAsset(
            id=uuid.uuid4(),
            owner_id=owner_id,
            object_key="",  # 下面按 id 生成，先占位
            mime=payload.mime,
            size=payload.size,
            idempotency_key=idempotency_key,
            input_hash=digest,
        )
        # 主键必须在这里定下来：列默认值要等 flush 才生效，key 生成时拿不到会变成 None
        asset.object_key = f"quarantine/{asset.id}"
        self.repo.add(asset)
        try:
            self.db.flush()
        except IntegrityError:
            # 并发同 key：唯一约束只让一个赢，输的一方回查赢家再决定重放还是 409
            self.db.rollback()
            winner = self.repo.get_by_owner_and_key(
                owner_id=owner_id, idempotency_key=idempotency_key
            )
            if winner is None:
                raise
            return self._replay(winner, digest)
        return self._intent_out(asset)

    # ---- 确认上传 ----

    def complete(
        self, *, owner_id: uuid.UUID, asset_id: uuid.UUID, idempotency_key: str
    ) -> MediaAssetOut:
        asset = self._owned(owner_id, asset_id)
        if asset.scan_state is not ScanState.PENDING:
            # 重复确认直接返回当前状态：complete 对同一资产是幂等的
            return self._to_out(asset)

        meta = self.storage.head(asset.object_key)
        if meta is None:
            raise Conflict("尚未检测到上传的文件", code="MEDIA_NOT_UPLOADED")
        if meta.size != asset.size:
            self._reject(asset)
            raise ValidationFailed(
                "实际文件大小与申请不一致",
                code="MEDIA_SIZE_MISMATCH",
                details=[{"field": "size", "reason": f"申请 {asset.size}，实际 {meta.size}"}],
            )
        data = self.storage.get(asset.object_key)
        if _sniff_mime(data) != asset.mime:
            self._reject(asset)
            raise ValidationFailed(
                "文件内容与声明的类型不一致",
                code="MEDIA_MIME_MISMATCH",
                details=[{"field": "mime", "reason": f"声明 {asset.mime}，内容不是该格式"}],
            )

        asset.checksum = meta.checksum
        asset.scan_state = ScanState.SCANNING
        self.db.flush()
        return self._to_out(asset)

    # ---- 查询 / 扫描回写 ----

    def get(self, *, owner_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAssetOut:
        return self._to_out(self._owned(owner_id, asset_id))

    def mark_clean(self, asset_id: uuid.UUID) -> None:
        """扫描通过：把对象挪进清洁区，之后才允许对外提供 URL。

        ME-02 的扫描器与测试都走这里；真实扫描（魔数/EXIF/缩略图）在 ME-02 里做。
        """
        asset = self.repo.get(asset_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        data = self.storage.get(asset.object_key) or b""
        new_key = f"clean/{asset.id}"
        self.storage.put(new_key, data)
        if new_key != asset.object_key:
            self.storage.delete(asset.object_key)
        asset.object_key = new_key
        asset.zone = MediaZone.CLEAN
        asset.scan_state = ScanState.CLEAN
        self.db.flush()

    # ---- 扫描（ME-02，由 worker 批量调用）----

    def scan(self, asset_id: uuid.UUID) -> MediaAsset:
        """解码重编码、清 EXIF、生成缩略图，过检才挪进 clean 区。

        幂等：已 clean / 已 rejected 的资产直接返回，不重复写对象。
        可预期的问题一律落到 REJECTED，不抛 500 —— 一张烂图不该让整批任务停摆。
        """
        asset = self.repo.get(asset_id)
        if asset is None:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        if asset.scan_state is ScanState.PENDING:
            raise Conflict("文件尚未确认上传", code="MEDIA_NOT_UPLOADED")
        if asset.scan_state in (ScanState.CLEAN, ScanState.REJECTED):
            return asset

        raw = self.storage.get(asset.object_key) or b""
        try:
            outcome = scan_image(raw, asset.mime)
        except ScanRejected:
            asset.scan_state = ScanState.REJECTED
            # commit 而不是 flush：拒绝结论必须存活到调用方之外（异常/回滚都不该撤销它）
            self.db.commit()
            return asset

        clean_key = f"clean/{asset.id}"
        thumb_key = f"{clean_key}.thumb.webp"
        self.storage.put(clean_key, outcome.data)
        self.storage.put(thumb_key, outcome.thumbnail)
        if asset.object_key != clean_key:
            self.storage.delete(asset.object_key)

        asset.object_key = clean_key
        asset.zone = MediaZone.CLEAN
        asset.scan_state = ScanState.CLEAN
        asset.mime = outcome.mime
        asset.size = len(outcome.data)
        asset.checksum = hashlib.sha256(outcome.data).hexdigest()
        asset.width = outcome.width
        asset.height = outcome.height
        self.db.flush()
        return asset

    def scan_pending(self, *, limit: int = 10) -> list[MediaAsset]:
        results: list[MediaAsset] = []
        for asset in self.repo.list_pending_scan(limit):
            try:
                results.append(self.scan(asset.id))
            except Conflict:
                # pending 资产理论上进不了这个列表，真撞上就跳过，别让 worker 崩
                continue
        return results

    # ---- 内部 ----

    def _owned(self, owner_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAsset:
        asset = self.repo.get(asset_id)
        # 不是本人的资产按"不存在"处理，不泄露存在性
        if asset is None or asset.owner_id != owner_id:
            raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
        return asset

    def _reject(self, asset: MediaAsset) -> None:
        asset.scan_state = ScanState.REJECTED
        # 拒绝要落库再抛错：否则异常把事务回滚，坏文件会停在待确认状态反复尝试
        self.db.commit()

    def _replay(self, asset: MediaAsset, digest: str) -> MediaIntentOut:
        if asset.input_hash != digest:
            raise Conflict(
                "幂等键已用于不同请求", code="IDEMPOTENCY_KEY_REUSED", details=_REUSE_DETAIL
            )
        return self._intent_out(asset)

    def _intent_out(self, asset: MediaAsset) -> MediaIntentOut:
        ticket = self.storage.presign_put(asset.object_key, mime=asset.mime, max_bytes=asset.size)
        return MediaIntentOut(
            asset_id=asset.id,
            upload_url=ticket.url,
            fields=ticket.fields,
            expires_at=ticket.expires_at,
        )

    def _to_out(self, asset: MediaAsset) -> MediaAssetOut:
        readable = asset.zone is MediaZone.CLEAN and asset.scan_state is ScanState.CLEAN
        url = f"{settings.api_prefix}/media/{asset.id}/raw" if readable else None
        return MediaAssetOut(
            id=asset.id,
            zone=asset.zone,
            scan_state=asset.scan_state,
            url=url,
            width=asset.width,
            height=asset.height,
        )


def require_clean_assets(
    db: Session, *, owner_id: uuid.UUID, asset_ids: list[uuid.UUID], field: str
) -> None:
    """校验一组媒体 id 都能被当前用户引用：存在、属于本人、扫描通过（RT-05）。

    这是本模块对外的只读契约，routes 不直接碰 media 的 repository——
    "什么算可用资产"的口径只在这里定义，另处复制一份迟早漂移。
    一次性把错误都收进 details：前端可以逐项高亮，而不是改一个报一个。
    """
    if not asset_ids:
        return
    found = {asset.id: asset for asset in MediaRepository(db).get_many(asset_ids)}
    details: list[dict[str, str]] = []
    for asset_id in asset_ids:
        asset = found.get(asset_id)
        if asset is None or asset.owner_id != owner_id:
            details.append({"field": field, "reason": "媒体不存在或无权使用"})
        elif asset.scan_state is not ScanState.CLEAN or asset.zone is not MediaZone.CLEAN:
            details.append({"field": field, "reason": "媒体尚未通过扫描"})
    if details:
        raise ValidationFailed("引用的媒体不可用", details=details)
