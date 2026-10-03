"""models/thumbnail.py — Thumbnail pool entry per channel."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class Thumbnail(Base, TimestampMixin):
    __tablename__ = "thumbnails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("youtube_channels.id", ondelete="CASCADE"), nullable=False, index=True)

    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=1280)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=720)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Ordering and state
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="thumbnails")

    @property
    def web_url(self) -> str:
        return f"/api/thumbnails/{self.id}/image"

    def __repr__(self) -> str:
        return f"<Thumbnail id={self.id} filename={self.filename!r}>"
