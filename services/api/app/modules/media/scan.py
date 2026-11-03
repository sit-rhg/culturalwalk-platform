# 媒体资源 · 图片扫描与重编码
"""扫描判定全部收在这一个文件里：worker 与 service 都只调用 scan_image()。

为什么必须重编码：原样保存等于把上传者写的 EXIF/GPS/ICC/注释一并发布出去；
重编码后只剩像素，缩略图也不例外——**任何一步都不把 exif/pnginfo 参数传给 save**。

拒绝码（供 service 落 scan_state=rejected 与审计）：
- MEDIA_EMPTY             空文件
- MEDIA_UNDECODABLE       不是能解出来的图片
- MEDIA_FORMAT_UNSUPPORTED 能解，但不是 jpeg/png/webp
- MEDIA_MIME_MISMATCH     实际格式与申请时的 Content-Type 不符
- MEDIA_TOO_LARGE          像素或边长超限（防解压炸弹）
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

# 12000 × 12000 已经远超手机拍照；再大基本可判定为解压炸弹。
MAX_SIDE = 12_000
MAX_PIXELS = 40_000_000

# 缩略图最长边（px）：草稿页列表用，够清晰又不至于把移动端拖慢。
THUMB_MAX_PX = 320

_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

_SUFFIX = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class ScanRejected(Exception):
    """不满足发布条件；service 捕获后把资产标成 rejected。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ScanOutcome:
    """扫描通过的产物：重编码后的字节 + 真实元信息 + 缩略图。"""

    data: bytes
    mime: str
    width: int
    height: int
    thumbnail: bytes


def scan_image(raw: bytes, declared_mime: str, *, thumb_max_px: int = THUMB_MAX_PX) -> ScanOutcome:
    """校验并清洗一张图片；不通过一律抛 ScanRejected。"""
    if not raw:
        raise ScanRejected("MEDIA_EMPTY", "文件为空")

    # 第一遍只验证结构：verify() 之后对象不可再用，所以第二遍重新 open。
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ScanRejected("MEDIA_UNDECODABLE", "图片无法解码") from exc

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            mime = _FORMATS.get(image.format or "")
            if mime is None:
                raise ScanRejected(
                    "MEDIA_FORMAT_UNSUPPORTED", f"不支持的图片格式：{image.format or '未知'}"
                )
            if mime != declared_mime:
                raise ScanRejected(
                    "MEDIA_MIME_MISMATCH", f"文件实际是 {mime}，与声明的 {declared_mime} 不符"
                )
            width, height = image.size
            if max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                raise ScanRejected(
                    "MEDIA_TOO_LARGE",
                    f"图片过大：{width}×{height}，上限 {MAX_SIDE}px / {MAX_PIXELS} 像素",
                )
            return ScanOutcome(
                data=_reencode(image, mime),
                mime=mime,
                width=width,
                height=height,
                thumbnail=_thumbnail(image, thumb_max_px),
            )
    except ScanRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ScanRejected("MEDIA_UNDECODABLE", "图片无法解码") from exc


def suffix_for(mime: str) -> str:
    return _SUFFIX.get(mime, "bin")


def _reencode(image: Image.Image, mime: str) -> bytes:
    """按真实格式重新编码，不携带任何元数据。"""
    buffer = io.BytesIO()
    if mime == "image/jpeg":
        image.convert("RGB").save(buffer, format="JPEG", quality=85, optimize=True)
    elif mime == "image/png":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="WEBP", quality=85, method=4)
    return buffer.getvalue()


def _thumbnail(image: Image.Image, max_px: int) -> bytes:
    thumb = image.copy()
    thumb.thumbnail((max_px, max_px))
    if thumb.mode not in ("RGB", "RGBA"):
        thumb = thumb.convert("RGBA" if "A" in thumb.getbands() else "RGB")
    buffer = io.BytesIO()
    thumb.save(buffer, format="WEBP", quality=80, method=4)
    return buffer.getvalue()
