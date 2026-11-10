# 媒体资源 · 对象存储读写适配器
"""签名器（storage.py）只负责签发上传地址，不碰字节。

扫描 worker 需要把原件读出来、把重编码后的副本写回去，所以另有一层最小的
「对象存储读写」协议。本机没有 MinIO 时用 LocalObjectStore（落盘到
settings.media_storage_root），接真对象存储时换实现即可，service/worker 不动。

key 就是桶内路径（如 `quarantine/<owner>/<asset>/photo.jpg`）；LocalObjectStore
把 key 映射成相对路径，`..` 越界一律拒绝，避免用数据库里的脏 key 读写到别处。
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.modules.media.adapters.storage import signing_key_for

_DEFAULT_REGION = "us-east-1"


class ObjectNotFound(Exception):
    """对象存储里没有这个 key；service 会转成 409，而不是当成扫描失败。"""


class ObjectStoreRejected(Exception):
    """key 非法（越界、绝对路径等）；属于编程错误，不该静默兜底。"""


class ObjectStore(Protocol):
    def get(self, key: str) -> bytes: ...

    def put(self, key: str, data: bytes) -> None: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    """开发/测试用实现：root 下的普通文件。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        candidate = (root / key).resolve()
        # 必须真的落在 root 里；`clean/../../etc/passwd` 这类 key 直接拒绝。
        if candidate != root and root not in candidate.parents:
            raise ObjectStoreRejected(f"对象 key 越界：{key}")
        return candidate

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(key)
        return path.read_bytes()

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ObjectStore:
    """真对象存储读写：SigV4 头签名 + urllib，不引 boto3。

    和 storage.py 的签名器共用同一套密钥与 region：签名器给前端签上传地址，
    这里给 worker 签 GET/PUT/DELETE。两边后端不一致时会出现「前端传成功、
    worker 读不到」，所以工厂函数只按 settings.storage_backend 选一次。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = _DEFAULT_REGION,
        timeout: float = 10.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout = timeout

    # ---- 内部：拼 URL 与签名 ----

    def _url(self, key: str) -> str:
        quoted = urllib.parse.quote(key, safe="/")
        return f"{self.endpoint}/{self.bucket}/{quoted}"

    def _signed_request(self, method: str, key: str, data: bytes | None = None) -> bytes | None:
        url = self._url(key)
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = amz_date[:8]
        payload = data if data is not None else b""
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_headers = (
            f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [
                method,
                urllib.parse.quote(parsed.path, safe="/"),
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{day}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = signing_key_for(self.secret_key, day=day, region=self.region)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read() if method != "HEAD" else b""
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method in {"GET", "HEAD"}:
                raise ObjectNotFound(key) from exc
            raise ObjectStoreRejected(f"对象存储返回 {exc.code}（{method} {key}）") from exc
        except urllib.error.URLError as exc:
            raise ObjectStoreRejected(f"对象存储不可达：{exc.reason}") from exc

    # ---- ObjectStore 协议 ----

    def get(self, key: str) -> bytes:
        body = self._signed_request("GET", key)
        return body or b""

    def put(self, key: str, data: bytes) -> None:
        self._signed_request("PUT", key, data)

    def delete(self, key: str) -> None:
        try:
            self._signed_request("DELETE", key)
        except ObjectStoreRejected as exc:
            # 删除不存在的 key 在 S3 是幂等成功；只有鉴权/网络错误该暴露出去。
            if "返回 404" not in str(exc):
                raise

    def exists(self, key: str) -> bool:
        try:
            self._signed_request("HEAD", key)
        except ObjectNotFound:
            return False
        return True


def get_object_store() -> ObjectStore:
    """唯一选择实现的地方：签名器与读写必须同后端（local 落盘 / s3 走网络）。"""
    if settings.storage_backend == "s3":
        return S3ObjectStore(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
    return LocalObjectStore(settings.media_storage_root)
