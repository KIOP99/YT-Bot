"""models/channel.py — YouTube channel with encrypted OAuth tokens."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class YouTubeChannel(Base, TimestampMixin):
    __tablename__ = "youtube_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    youtube_channel_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    subscriber_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    custom_url: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Encrypted OAuth tokens (Fernet)
    encrypted_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # OAuth state for the PKCE/redirect flow
    oauth_state: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    videos: Mapped[list["Video"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    thumbnails: Mapped[list["Thumbnail"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    schedule_config: Mapped[Optional["ScheduleConfig"]] = relationship(back_populates="channel", uselist=False, cascade="all, delete-orphan")
    upload_history: Mapped[list["UploadHistory"]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    discord_config: Mapped[Optional["DiscordConfig"]] = relationship(back_populates="channel", uselist=False, cascade="all, delete-orphan")
    discord_bot: Mapped[Optional["DiscordBot"]] = relationship(back_populates="channel", uselist=False, cascade="all, delete-orphan")
    comment_config: Mapped[Optional["CommentConfig"]] = relationship(back_populates="channel", uselist=False, cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<YouTubeChannel id={self.id} name={self.name!r}>"
