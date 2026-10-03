"""
core/config.py
--------------
Pydantic-based settings management. All values are read from environment
variables (or a .env file). Import `settings` anywhere in the project.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ─────────────────────────────────────────────
    app_env: Literal["development", "production"] = "development"
    app_secret_key: str = Field(..., min_length=32)
    app_host: str = "0.0.0.0"
    app_port: int = 19232
    app_base_url: str = "http://og.yaddu.net:19232"

    @model_validator(mode="before")
    @classmethod
    def check_container_ports(cls, values: Any) -> Any:
        if isinstance(values, dict):
            # If server is running in Pterodactyl / Docker with dynamic port
            env_port = os.getenv("SERVER_PORT") or os.getenv("PORT")
            if env_port and "app_port" not in values:
                try:
                    values["app_port"] = int(env_port)
                except ValueError:
                    pass
        return values

    # ── Database ────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./ytbot.db"

    # ── Admin (bootstrap credentials) ───────────────────────────
    admin_username: str = "admin"
    admin_password: str = Field(default="8492", min_length=2)

    # ── Credential Rotation ──────────────────────────────────────
    cred_rotation_interval_hours: int = 168
    cred_rotation_notify_dm: bool = True

    # ── JWT ──────────────────────────────────────────────────────
    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    session_expire_minutes: int = 1440

    # ── Fernet encryption ────────────────────────────────────────
    fernet_key: str = Field(..., min_length=44)

    # ── Discord ──────────────────────────────────────────────────
    discord_bot_token: str
    discord_admin_user_id: int
    discord_guild_id: int
    discord_announcement_channel_id: int

    # ── Google OAuth ─────────────────────────────────────────────
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str = "http://og.yaddu.net:19232/api/channels/oauth/callback"

    # ── Scheduling ───────────────────────────────────────────────
    scheduler_timezone: str = "US/Eastern"
    upload_window_start: int = Field(14, ge=0, le=23)
    upload_window_end: int = Field(20, ge=0, le=23)
    processing_lead_minutes: int = 30

    # ── FFmpeg ───────────────────────────────────────────────────
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # ── Random Overlay Words ──────────────────────────────────────
    random_words_list: str = "abc,free,pro,win,vip,top,star,fast,safe,cool,play,hero,mega,best,gold,fire,live,game,zone,link,easy,boss,luck,rush,pure,nova,apex,neon,open,gift,drop,epic,flow,wave,peak,zoom,power,prime,super,bonus,ace,gem,coin,pass,wild,loot,team,club,cast,glow,spark,flash,craft,ultra,byte,core,iron,rock,sky,red,blue,xyz"

    # ── LicenseAuth / KeyAuth Seller API ──────────────────────────
    licenseauth_seller_key: str = "5910e2c62eddc63f027d530ba8578fb9"
    licenseauth_api_url: str = "https://licenseauth.help/api/seller/"
    licenseauth_sub_name: str = "default"
    licenseauth_expiry_days: int = 1
    licenseauth_auto_create: bool = True


    # ── Rate Limiting ────────────────────────────────────────────
    rate_limit_login: str = "5/minute"
    rate_limit_api: str = "60/minute"

    # ── File Limits ──────────────────────────────────────────────
    max_video_size_mb: int = 4096
    max_thumbnail_size_mb: int = 10

    # ── 2FA ──────────────────────────────────────────────────────
    totp_enabled: bool = False
    totp_issuer: str = "YTBotDashboard"

    # ── Logging ──────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = "logs/ytbot.log"

    # ── Computed helpers ─────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def max_video_size_bytes(self) -> int:
        return self.max_video_size_mb * 1024 * 1024

    @property
    def max_thumbnail_size_bytes(self) -> int:
        return self.max_thumbnail_size_mb * 1024 * 1024

    @field_validator("upload_window_end")
    @classmethod
    def end_after_start(cls, v: int, info) -> int:
        start = info.data.get("upload_window_start", 0)
        if v <= start:
            raise ValueError("upload_window_end must be after upload_window_start")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Convenience singleton — use `from core.config import settings`
settings: Settings = get_settings()
