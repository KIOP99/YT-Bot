"""models/discord_config.py — Per-channel Discord announcement settings."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin

DEFAULT_TEMPLATE = (
    "🎬 **New video uploaded!**\n\n"
    "**{title}**\n"
    "{url}\n\n"
    "📅 Uploaded on {date} to **{channel_name}**"
)


class DiscordConfig(Base, TimestampMixin):
    __tablename__ = "discord_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    discord_channel_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    webhook_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    announcement_template: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_TEMPLATE
    )
    include_thumbnail: Mapped[bool] = mapped_column(Boolean, default=True)
    ping_role_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Components V2 and custom card styles
    heading_title: Mapped[Optional[str]] = mapped_column(String(128), default="▶️ WATCH VIDEO NOW")
    custom_video_title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    button_label: Mapped[Optional[str]] = mapped_column(String(64), default="▶️ PLAY VIDEO NOW")
    button_label_2: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="💬 Join Discord Server")
    button_url_2: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default="https://discord.gg/ApzZFmQDBd")
    accent_color: Mapped[Optional[str]] = mapped_column(String(16), default="#00A2C7")
    features_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    footer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    use_components_v2: Mapped[bool] = mapped_column(Boolean, default=True)
    button_in_box: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Convenience properties and edit methods ───────────────────────────────
    @property
    def discord_invite_url(self) -> Optional[str]:
        """Discord Server Invite Link (e.g., https://discord.gg/ApzZFmQDBd). Maps to button_url_2."""
        return self.button_url_2

    @discord_invite_url.setter
    def discord_invite_url(self, value: Optional[str]) -> None:
        self.button_url_2 = value.strip() if value else None

    @property
    def discord_server_url(self) -> Optional[str]:
        """Discord Server Invite Link (e.g., https://discord.gg/ApzZFmQDBd). Maps to button_url_2."""
        return self.button_url_2

    @discord_server_url.setter
    def discord_server_url(self, value: Optional[str]) -> None:
        self.button_url_2 = value.strip() if value else None

    def set_discord_invite(self, invite_url: str, label: str = "💬 Join Discord Server") -> None:
        """
        Method to update the Discord Server invite link and button title.
        Example: config.set_discord_invite("https://discord.gg/ApzZFmQDBd", "💬 Join Discord Server")
        """
        self.button_url_2 = invite_url.strip() if invite_url else None
        if label:
            self.button_label_2 = label.strip()

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="discord_config")

    def __repr__(self) -> str:
        return f"<DiscordConfig channel_id={self.channel_id} discord_server_url={self.button_url_2}>"
