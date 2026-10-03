"""models/video.py — Managed video file per channel with timestamp text overlays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class Video(Base, TimestampMixin):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("youtube_channels.id", ondelete="CASCADE"), nullable=False, index=True)

    # File info
    original_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # YouTube metadata
    title: Mapped[str] = mapped_column(String(100), nullable=False, default="My Video")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON array string
    category_id: Mapped[str] = mapped_column(String(8), nullable=False, default="22")  # 22 = People & Blogs
    privacy_status: Mapped[str] = mapped_column(String(16), nullable=False, default="public")

    # FFmpeg settings
    apply_ffmpeg: Mapped[bool] = mapped_column(Boolean, default=False)
    intro_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    outro_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    trim_start: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # seconds
    trim_end: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Username Overlay / Watermark at specific timestamp ────────────
    overlay_username_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    overlay_username_text: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, default="User: {username}")
    overlay_username_start: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=5.0)
    overlay_username_end: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=15.0)
    overlay_username_position: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="safe-top-left")
    overlay_username_font_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=36)
    overlay_username_color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="#ffffff")
    overlay_randomize_username: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Password / Pass Overlay at specific timestamp ─────────────────
    overlay_password_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    overlay_password_text: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, default="Pass: {password}")
    overlay_password_start: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=20.0)
    overlay_password_end: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=30.0)
    overlay_password_position: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="safe-bottom-right")
    overlay_password_font_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=36)
    overlay_password_color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="#22c55e")
    overlay_randomize_password: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Random Time Window for Username & Password ────────────────────
    overlay_randomize_time: Mapped[bool] = mapped_column(Boolean, default=False)
    overlay_random_u_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=5.0)
    overlay_random_u_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=30.0)
    overlay_random_p_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=25.0)
    overlay_random_p_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=60.0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Thumbnail Association ──────────────────────────────────────────
    thumbnail_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("thumbnails.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="videos")
    thumbnail: Mapped[Optional["Thumbnail"]] = relationship(
        "Thumbnail",
        foreign_keys=[thumbnail_id],
        lazy="selectin",
    )

    @property
    def web_url(self) -> str:
        clean_name = Path(self.stored_path).name
        return f"/uploads/videos/{clean_name}"

    @property
    def thumbnail_web_url(self) -> str:
        try:
            if getattr(self, "thumbnail_id", None):
                return f"/api/thumbnails/{self.thumbnail_id}/image"
            from sqlalchemy.orm import attributes
            state = attributes.instance_state(self)
            if "thumbnail" in state.dict and state.dict["thumbnail"]:
                return state.dict["thumbnail"].web_url
            if "channel" in state.dict and state.dict["channel"]:
                ch = state.dict["channel"]
                ch_state = attributes.instance_state(ch)
                if "thumbnails" in ch_state.dict and ch_state.dict["thumbnails"]:
                    active_pool = [t for t in ch_state.dict["thumbnails"] if getattr(t, "is_active", False) and not getattr(t, "filename", "").startswith("auto_video_")]
                    if active_pool:
                        return active_pool[0].web_url
        except Exception:
            pass
        return ""

    @property
    def thumbnail_name(self) -> str:
        try:
            if getattr(self, "thumbnail_id", None):
                from sqlalchemy.orm import attributes
                state = attributes.instance_state(self)
                if "thumbnail" in state.dict and state.dict["thumbnail"]:
                    return state.dict["thumbnail"].filename
                return f"Thumbnail #{self.thumbnail_id}"
            from sqlalchemy.orm import attributes
            state = attributes.instance_state(self)
            if "channel" in state.dict and state.dict["channel"]:
                ch = state.dict["channel"]
                ch_state = attributes.instance_state(ch)
                if "thumbnails" in ch_state.dict and ch_state.dict["thumbnails"]:
                    active_pool = [t for t in ch_state.dict["thumbnails"] if getattr(t, "is_active", False) and not getattr(t, "filename", "").startswith("auto_video_")]
                    if active_pool:
                        return f"{active_pool[0].filename} (Stock)"
        except Exception:
            pass
        return "Channel Stock (Auto-Rotate)"

    @property
    def safe_tags_list(self) -> list[str]:
        try:
            return json.loads(self.tags)
        except Exception:
            return []

    def __repr__(self) -> str:
        return f"<Video id={self.id} title={self.title!r}>"

