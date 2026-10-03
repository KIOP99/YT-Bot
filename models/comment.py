"""models/comment.py — Auto-comment settings, template pool, and history per channel."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin

DEFAULT_COMMENT_TEMPLATE = (
    "Thanks for watching! Don't forget to like and subscribe for more daily videos!\n"
    "Video: {title}\n"
    "Watch link: {url}\n"
    "Uploaded on: {date}"
)


class CommentConfig(Base, TimestampMixin):
    __tablename__ = "comment_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Rotation mode: "sequential" | "random"
    rotation_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="sequential")
    # Delay after publication in seconds (e.g. 0 to 300)
    delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    last_template_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="comment_config")
    templates: Mapped[List["CommentTemplate"]] = relationship(
        back_populates="config",
        cascade="all, delete-orphan",
        order_by="CommentTemplate.order_index",
    )

    def __repr__(self) -> str:
        return f"<CommentConfig channel_id={self.channel_id} enabled={self.is_enabled} mode={self.rotation_mode}>"


class CommentTemplate(Base, TimestampMixin):
    __tablename__ = "comment_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("comment_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Relationship
    config: Mapped["CommentConfig"] = relationship(back_populates="templates")

    def __repr__(self) -> str:
        return f"<CommentTemplate id={self.id} channel_id={self.channel_id} active={self.is_active}>"


class CommentHistory(Base, TimestampMixin):
    __tablename__ = "comment_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    video_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL"),
        nullable=True,
    )
    youtube_video_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    comment_text: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_comment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")  # "success", "failed", "pending"
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posted_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship()
    video: Mapped[Optional["Video"]] = relationship()

    def __repr__(self) -> str:
        return f"<CommentHistory id={self.id} yt_video_id={self.youtube_video_id} status={self.status}>"
