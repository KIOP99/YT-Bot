"""models/upload_history.py — Record of every upload attempt."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class UploadHistory(Base, TimestampMixin):
    __tablename__ = "upload_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Outcome
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # "pending" | "processing" | "uploading" | "success" | "failed"

    youtube_video_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    youtube_url: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    video_title: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    thumbnail_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("thumbnails.id", ondelete="SET NULL"), nullable=True
    )

    scheduled_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    started_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Error info
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Processing duration in seconds
    processing_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    upload_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Discord announcement message ID
    discord_message_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="upload_history")
    thumbnail: Mapped[Optional["Thumbnail"]] = relationship()

    @property
    def thumbnail_display_url(self) -> Optional[str]:
        """Return active thumbnail image URL for UI preview, falling back to YouTube CDN."""
        if self.thumbnail and hasattr(self.thumbnail, "web_url"):
            return self.thumbnail.web_url
        if self.youtube_video_id:
            return f"https://img.youtube.com/vi/{self.youtube_video_id}/mqdefault.jpg"
        return None

    def __repr__(self) -> str:
        return f"<UploadHistory id={self.id} status={self.status!r}>"
