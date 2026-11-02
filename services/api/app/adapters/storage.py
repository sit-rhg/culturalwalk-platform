# CulturalWalk API · 对象存储适配器
"""对象存储是唯一允许出现"外部系统"的边界之一（另一个是地图/LLM Provider）。

规则（docs/CulturalWalk_VibeCoding底层架构与开发范式.md §11.1.1）：
- 浏览器直传，API 不代理用户任意 URL（避免 SSRF）；
- 上传 URL 只绑定一个 object key，带方法/大小/类型/TTL 限额；
- 预览/下载只能走 clean 区，quarantine 只对内部扫描器可见。

本地适配器把对象落在磁盘上，签名用 HMAC，够开发/CI 用；
S3/MinIO 适配器在 SigV4 与真实桶联调完成前一律 fail-closed——宁可 503，
也不要返回一个"看起来像预签名、其实谁都能写任意 key"的地址。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.errors import AppError


class StorageUnavailable(AppError):
    """存储未配置或尚未接入：调用方应降级而不是继续假装成功。"""

    http_status = 503
    code = "STORAGE_UNAVAILABLE"


class StorageRejected(AppError):
    """签名过期 / key 不符 / 超出声明大小。"""

    http_status = 403
    code = "STORAGE_SIGNATURE_INVALID"


@dataclass(frozen=True)
class ObjectMeta:
    key: str
    size: int
    checksum: str  # sha256 hex


@dataclass(frozen=True)
class UploadTicket:
    url: str
    fields: dict[str, str]
    expires_at: datetime


class StorageAdapter(Protocol):
    def presign_put(self, key: str, *, mime: str, max_bytes: int) -> UploadTicket: ...

    def head(self, key: str) -> ObjectMeta | None: ...

    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, data: bytes) -> ObjectMeta: ...

    def public_url(self, key: str) -> str | None: ...

    def delete(self, key: str) -> None: ...


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalStorageAdapter:
    """磁盘适配器：开发与 CI 的默认实现。

    签名 = HMAC(secret, "put:<key>:<expires>:<mime>:<max_bytes>")。验证时把
    全部参数重新算一遍，所以改 key、改 MIME、改大小上限、改过期时间都会失效。
    """

    def __init__(
        self,
        *,
        root: Path | str,
        secret: str,
        public_base_url: str,
        ttl_seconds: int = 600,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode("utf-8")
        self.public_base_url = public_base_url.rstrip("/")
        self.ttl_seconds = ttl_seconds

    # ---- 内部 ----
    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise StorageRejected("对象 key 越界")
        return candidate

    def _sign(self, *, method: str, key: str, expires: int, mime: str, max_bytes: int) -> str:
        payload = f"{method}:{key}:{expires}:{mime}:{max_bytes}".encode()
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    # ---- 端口 ----
    def presign_put(self, key: str, *, mime: str, max_bytes: int) -> UploadTicket:
        expires = int(time.time()) + self.ttl_seconds
        signature = self._sign(
            method="put", key=key, expires=expires, mime=mime, max_bytes=max_bytes
        )
        url = (
            f"{self.public_base_url}/_media/upload/{key}"
            f"?expires={expires}&mime={mime}&max_bytes={max_bytes}&signature={signature}"
        )
        return UploadTicket(
            url=url,
            fields={"Content-Type": mime},
            expires_at=datetime.fromtimestamp(expires, tz=UTC),
        )

    def verify_put(
        self, *, key: str, expires: int, mime: str, max_bytes: int, signature: str
    ) -> bool:
        if expires < int(time.time()):
            return False
        expected = self._sign(
            method="put", key=key, expires=expires, mime=mime, max_bytes=max_bytes
        )
        return hmac.compare_digest(expected, signature)

    def put(self, key: str, data: bytes) -> ObjectMeta:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ObjectMeta(key=key, size=len(data), checksum=_sha256_hex(data))

    def head(self, key: str) -> ObjectMeta | None:
        path = self._path(key)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return ObjectMeta(key=key, size=len(data), checksum=_sha256_hex(data))

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def public_url(self, key: str) -> str | None:
        return f"{self.public_base_url}/_media/objects/{key}"

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()


class S3StorageAdapter:
    """S3/MinIO 适配器（未接入）。"""

    def __init__(self, *, endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key

    def _unavailable(self) -> StorageUnavailable:
        return StorageUnavailable(
            "对象存储尚未接入：缺少 SigV4 预签名实现与 MinIO 联调，暂用 LOCAL 适配器",
        )

    def presign_put(self, key: str, *, mime: str, max_bytes: int) -> UploadTicket:
        raise self._unavailable()

    def head(self, key: str) -> ObjectMeta | None:
        raise self._unavailable()

    def get(self, key: str) -> bytes | None:
        raise self._unavailable()

    def put(self, key: str, data: bytes) -> ObjectMeta:
        raise self._unavailable()

    def public_url(self, key: str) -> str | None:
        raise self._unavailable()

    def delete(self, key: str) -> None:
        raise self._unavailable()


def build_storage() -> StorageAdapter:
    if settings.media_storage == "local":
        return LocalStorageAdapter(
            root=settings.media_storage_root,
            secret=settings.media_storage_secret,
            public_base_url=settings.media_public_base_url,
            ttl_seconds=settings.media_upload_ttl_seconds,
        )
    if settings.media_storage == "s3":
        return S3StorageAdapter(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
    raise StorageUnavailable(f"未知的存储后端：{settings.media_storage}")


_storage: StorageAdapter | None = None


def get_storage() -> StorageAdapter:
    """FastAPI 依赖；测试里用 `app.dependency_overrides` 换成 tmp_path 适配器。"""
    global _storage
    if _storage is None:
        _storage = build_storage()
    return _storage


def reset_storage() -> None:
    """测试或切换配置后重建单例。"""
    global _storage
    _storage = None
