# 媒体资源 · 上传签名适配器
"""把「申请上传」和「对象存储」隔开。

本机/测试环境没有 MinIO，因此默认实现是 **纯本地占位**：
按 S3 POST Policy 的形状拼出 upload_url / fields / x-amz-signature，
方便前端照契约联调，但**不连接对象存储、不创建桶、不会真的接受文件**。

接真存储时只换这个文件里的 signer（例如 boto3 `generate_presigned_post`），
service 与接口契约都不用改。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.config import settings

# 预签名地址有效期。够前端在弱网下重试一次，又不会让泄露的地址长期可用。
UPLOAD_TTL_SECONDS = 900

_DEFAULT_REGION = "us-east-1"


@dataclass(frozen=True)
class UploadTicket:
    """一次上传所需的全部信息；expires_at 是服务端生成的绝对时间，不是倒计时秒数。"""

    upload_url: str
    fields: dict[str, str]
    expires_at: datetime


class StorageSigner(Protocol):
    """签名器协议：service 只依赖它，不依赖 boto3/S3 细节。"""

    def presign_upload(
        self, *, object_key: str, mime: str, size: int, expires_at: datetime
    ) -> UploadTicket: ...


class PlaceholderS3Signer:
    """S3 POST Policy 形状的占位签名器（无网络 I/O）。

    签名算法也不是真正的 SigV4：真实 S3 需要按日期/区域/服务派生 signing key。
    这里只保证字段齐全、格式稳定、签名随 policy 变化，用来联调和跑测试。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = _DEFAULT_REGION,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region

    def presign_upload(
        self, *, object_key: str, mime: str, size: int, expires_at: datetime
    ) -> UploadTicket:
        issued_at = datetime.now(UTC)
        expiration = expires_at.astimezone(UTC)
        amz_date = issued_at.strftime("%Y%m%dT%H%M%SZ")
        day = issued_at.strftime("%Y%m%d")
        credential = f"{self.access_key}/{day}/{self.region}/s3/aws4_request"

        policy_document = {
            "expiration": expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conditions": [
                {"bucket": self.bucket},
                ["starts-with", "$key", "quarantine/"],
                {"Content-Type": mime},
                # 服务端声明的 size 只是提示；桶侧只兜住全局上限，避免客户端改字段绕过。
                ["content-length-range", 1, settings.media_max_bytes],
            ],
        }
        policy = base64.b64encode(
            json.dumps(policy_document, separators=(",", ":"), sort_keys=True).encode()
        ).decode()
        signing_key = hmac.new(
            self.secret_key.encode(), f"{day}/{self.region}/s3".encode(), hashlib.sha256
        ).digest()
        signature = hmac.new(signing_key, policy.encode(), hashlib.sha256).hexdigest()

        fields = {
            "key": object_key,
            "Content-Type": mime,
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": credential,
            "x-amz-date": amz_date,
            "policy": policy,
            "x-amz-signature": signature,
        }
        return UploadTicket(
            upload_url=f"{self.endpoint}/{self.bucket}",
            fields=fields,
            expires_at=expiration,
        )


def get_storage_signer() -> StorageSigner:
    """唯一选择实现的地方：换成 MinIO/OSS 客户端时只改这一处。"""
    return PlaceholderS3Signer(
        endpoint=settings.s3_endpoint,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )


def default_upload_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=UPLOAD_TTL_SECONDS)
