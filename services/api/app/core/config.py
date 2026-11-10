# CulturalWalk API · 核心配置
"""所有环境变量集中在这里读取，其他模块不许直接摸 os.environ。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库里的默认值：只够本机跑，一旦带进 demo/prod 就等于把签名密钥公开了。
_DEFAULT_JWT_SECRET = "dev-only-change-me"
_SHARED_ENVS = frozenset({"demo", "prod", "production"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用
    app_name: str = "CulturalWalk API"
    env: str = "local"  # local | demo | prod
    api_prefix: str = "/api/v1"
    debug: bool = True

    # 数据库
    database_url: str = "postgresql+psycopg://cw:cw@localhost:5432/culturalwalk"
    db_echo: bool = False

    # 认证
    jwt_secret: str = "dev-only-change-me"
    jwt_alg: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 14 * 24 * 3600

    # 跨域：本地 vite 默认端口。部署到别的域名时用 CORS_ORIGINS（JSON 数组）覆盖，
    # 不许把线上域名硬编码进代码。
    cors_origins: list[str] = ["http://localhost:5173"]

    # 对象存储
    # local = 落盘到 media_storage_root（只用于本机/演示）；s3 = 真对象存储。
    # 演示与生产必须显式选 s3 并给密钥，否则上传地址是签给本地占位实现的，
    # 前端拿着它上传会静默丢文件。
    storage_backend: str = "local"  # local | s3
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "culturalwalk"
    # 故意不再给 minioadmin 这类默认值：空串会让缺配置当场暴露，而不是用公开密钥跑起来。
    s3_access_key: str = ""
    s3_secret_key: str = ""
    media_max_bytes: int = 10 * 1024 * 1024
    # 本机对象存储根目录：扫描 worker 从这里读原件、写 clean 副本与缩略图。
    # 接真 MinIO/S3 时由对象存储读写适配器换实现，service 与 worker 不用改。
    media_storage_root: str = "var/media"

    # 外部 Provider（留空则走 fake 实现，便于无网演示）
    map_provider: str = "fake"  # fake | amap | tencent
    map_api_key: str = ""
    # 供应商坐标系/许可核验通过后才允许置 True；核验前接真实 Provider 只会写进偏移坐标
    map_datum_verified: bool = False
    map_timeout_seconds: float = 3.0
    llm_provider: str = "fake"  # fake | openai-compatible
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # 业务开关
    plan_max_days: int = 7
    review_lease_minutes: int = 30
    public_cache_seconds: int = 60

    # 打包运行时：构建好的前端目录（存在就由后端托管，省掉 vite dev server）
    web_dist: Path | None = None

    @model_validator(mode="after")
    def _reject_shared_defaults(self) -> Settings:
        """demo/prod 不许用仓库里的默认密钥。

        默认值在 git 里躺着，任何人都能拿它签一个 role=admin 的令牌 —— 这不是
        「记得改配置」的提醒，是启动即失败的硬闸；本地 local 环境不受影响。
        """
        if self.env not in _SHARED_ENVS:
            return self
        if self.jwt_secret == _DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32:
            raise ValueError(
                "非 local 环境必须通过 JWT_SECRET 提供至少 32 字符的独立密钥"
                "（当前仍是仓库默认值，任何人都能伪造管理员令牌）"
            )
        if self.storage_backend != "s3":
            raise ValueError(
                "非 local 环境必须使用 STORAGE_BACKEND=s3；local 后端把文件落本机磁盘，"
                "只适合开发与答辩演示"
            )
        if len(self.s3_access_key) < 8 or len(self.s3_secret_key) < 16:
            raise ValueError(
                "STORAGE_BACKEND=s3 必须提供 S3_ACCESS_KEY / S3_SECRET_KEY；"
                "仓库里不再保留 minioadmin 这类公开默认值"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
