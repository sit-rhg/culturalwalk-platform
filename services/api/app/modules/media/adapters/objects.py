# 媒体资源 · 对象存储读写适配器
"""签名器（storage.py）只负责签发上传地址，不碰字节。

扫描 worker 需要把原件读出来、把重编码后的副本写回去，所以另有一层最小的
「对象存储读写」协议。本机没有 MinIO 时用 LocalObjectStore（落盘到
settings.media_storage_root），接真对象存储时换实现即可，service/worker 不动。

key 就是桶内路径（如 `quarantine/<owner>/<asset>/photo.jpg`）；LocalObjectStore
把 key 映射成相对路径，`..` 越界一律拒绝，避免用数据库里的脏 key 读写到别处。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.config import settings


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


def get_object_store() -> ObjectStore:
    """唯一选择实现的地方；接 MinIO/S3 时只改这里。"""
    return LocalObjectStore(settings.media_storage_root)
