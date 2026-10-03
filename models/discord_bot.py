"""models/discord_bot.py — Per-channel Discord bot configuration."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class DiscordBot(Base, TimestampMixin):
    """
    Stores one Discord bot token (and branding) per YouTube channel.
    Each channel can have its own bot — completely independent bots
    that can run simultaneously.
    """

    __tablename__ = "discord_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Link to YouTube channel (one-to-one)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Bot credentials
    bot_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Branding — stored relative paths under uploads/bot_assets/
    avatar_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    banner_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Status
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="discord_bot")

    def __repr__(self) -> str:
        return f"<DiscordBot channel_id={self.channel_id} name={self.bot_name!r}>"
