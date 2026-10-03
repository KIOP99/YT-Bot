"""models/schedule_config.py — Per-channel scheduling settings."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class ScheduleConfig(Base, TimestampMixin):
    __tablename__ = "schedule_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")

    # Scheduling mode: "window" (random time in window) or "exact" (fixed time each day)
    schedule_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="exact")

    # Upload window (hours, 0–23) — used when schedule_mode == "window"
    window_start_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    window_end_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    # Exact upload time — used when schedule_mode == "exact"
    upload_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=14)    # 0-23
    upload_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # 0-59

    # How many minutes before upload time to start video processing (FFmpeg etc.)
    pre_process_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # Thumbnail rotation mode
    thumbnail_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sequential"
    )  # "sequential" | "random"
    thumbnail_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Next scheduled run (ISO string, stored in UTC)
    next_upload_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Next processing start time (ISO string, stored in UTC) — fires pre_process_minutes before upload
    next_process_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # APScheduler job ID for this channel
    scheduler_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Relationships
    channel: Mapped["YouTubeChannel"] = relationship(back_populates="schedule_config")

    def time_until_upload(self) -> Optional[dict]:
        """
        Calculate the time remaining until the scheduled upload.
        Returns a dict with total_seconds, days, hours, minutes, seconds, formatted, is_due,
        or None if next_upload_at is not set.
        """
        if not self.next_upload_at:
            return None
        return self._calculate_time_remaining(self.next_upload_at)

    def time_until_process(self) -> Optional[dict]:
        """
        Calculate the time remaining until pre-processing starts.
        Returns a dict with total_seconds, days, hours, minutes, seconds, formatted, is_due,
        or None if next_process_at is not set.
        """
        if not self.next_process_at:
            return None
        return self._calculate_time_remaining(self.next_process_at)

    @staticmethod
    def _calculate_time_remaining(iso_str: str) -> dict:
        """Parse an ISO UTC timestamp and calculate the countdown delta against current UTC time."""
        from datetime import datetime, timezone
        try:
            target_dt = datetime.fromisoformat(iso_str)
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            diff = target_dt - now_utc
            total_seconds = int(diff.total_seconds())

            if total_seconds <= 0:
                return {
                    "total_seconds": total_seconds,
                    "days": 0,
                    "hours": 0,
                    "minutes": 0,
                    "seconds": 0,
                    "is_due": True,
                    "formatted": "Due now",
                    "formatted_short": "00:00:00",
                }

            days = total_seconds // 86400
            rem = total_seconds % 86400
            hours = rem // 3600
            rem %= 3600
            minutes = rem // 60
            seconds = rem % 60

            parts = []
            if days > 0:
                parts.append(f"{days}d")
            if hours > 0 or days > 0:
                parts.append(f"{hours}h")
            if minutes > 0 or hours > 0 or days > 0:
                parts.append(f"{minutes}m")
            parts.append(f"{seconds}s")
            formatted = " ".join(parts)

            formatted_short = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            if days > 0:
                formatted_short = f"{days}d {formatted_short}"

            return {
                "total_seconds": total_seconds,
                "days": days,
                "hours": hours,
                "minutes": minutes,
                "seconds": seconds,
                "is_due": False,
                "formatted": formatted,
                "formatted_short": formatted_short,
            }
        except Exception:
            return {
                "total_seconds": 0,
                "days": 0,
                "hours": 0,
                "minutes": 0,
                "seconds": 0,
                "is_due": False,
                "formatted": "Unknown",
                "formatted_short": "--:--:--",
            }

    @property
    def formatted_time_until_upload(self) -> str:
        """Human-readable string of time remaining until upload (e.g. '2h 15m 30s' or 'Due now')."""
        res = self.time_until_upload()
        return res["formatted"] if res else "Not scheduled"

    @property
    def formatted_time_until_process(self) -> str:
        """Human-readable string of time remaining until pre-processing starts."""
        res = self.time_until_process()
        return res["formatted"] if res else "Not scheduled"

    def __repr__(self) -> str:
        return f"<ScheduleConfig channel_id={self.channel_id} active={self.is_active} mode={self.schedule_mode}>"

