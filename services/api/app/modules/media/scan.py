# 媒体资源 · 扫描与缩略图
"""ME-02：校验 → 解码 → 重编码 → 缩略图。

为什么必须重编码：只做「扩展名 + MIME 白名单」挡不住多态文件（图片头 + 尾随脚本、
内嵌 HTML、EXIF/GPS）。把像素真正解出来、按目标格式重新写一遍，附带的元数据和
尾随载荷都会自然消失；这也是 docs/…开发范式.md §11.1.2 第 3 步要求的做法。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_SIDE = 12000
MAX_PIXELS = 40_000_000
THUMB_MAX_PX = 320

_FORMAT_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_SAVE_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class ScanRejected(Exception):
    """可预期的拒绝：调用方据此把资产标成 REJECTED，而不是让整批任务崩掉。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ScanOutcome:
    data: bytes
    mime: str
    width: int
    height: int
    thumbnail: bytes


def _reencode(img: Image.Image, mime: str) -> bytes:
    buf = io.BytesIO()
    fmt = _SAVE_FORMAT[mime]
    if fmt == "JPEG":
        # JPEG 不支持透明通道，统一转 RGB；不传 exif/pnginfo，元数据不会被带过去
        img.convert("RGB").save(buf, format=fmt, quality=85, optimize=True)
    elif fmt == "PNG":
        img.save(buf, format=fmt, optimize=True)
    else:
        img.convert("RGB").save(buf, format=fmt, quality=85, method=4)
    return buf.getvalue()


def _thumbnail(img: Image.Image, max_px: int) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    thumb.convert("RGB").save(buf, format="WEBP", quality=80, method=4)
    return buf.getvalue()


def scan_image(raw: bytes, declared_mime: str, *, thumb_max_px: int = THUMB_MAX_PX) -> ScanOutcome:
    """把上传的字节变成可发布的 (干净原图, 缩略图, 尺寸) 或抛出 `ScanRejected`。"""
    if not raw:
        raise ScanRejected("MEDIA_EMPTY", "文件为空")

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()  # 只做结构校验，verify 之后对象不可再用
        img = Image.open(io.BytesIO(raw))
        img.load()  # 真正解码：截断/损坏的文件在这一步暴露
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ScanRejected("MEDIA_UNDECODABLE", "文件无法解码或已损坏") from exc
    except Image.DecompressionBombError as exc:  # 超大像素图，防内存放大
        raise ScanRejected("MEDIA_TOO_LARGE", "图片像素过大") from exc

    fmt = (img.format or "").upper()
    detected = _FORMAT_TO_MIME.get(fmt)
    if detected is None:
        raise ScanRejected("MEDIA_FORMAT_UNSUPPORTED", f"不支持的图片格式 {fmt or '未知'}")
    if detected != declared_mime:
        raise ScanRejected("MEDIA_MIME_MISMATCH", f"声明 {declared_mime}，实际是 {detected}")

    width, height = img.size
    if width <= 0 or height <= 0:
        raise ScanRejected("MEDIA_EMPTY", "图片尺寸为 0")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise ScanRejected("MEDIA_TOO_LARGE", f"{width}x{height} 超过允许的尺寸")

    return ScanOutcome(
        data=_reencode(img, detected),
        mime=detected,
        width=width,
        height=height,
        thumbnail=_thumbnail(img, thumb_max_px),
    )
