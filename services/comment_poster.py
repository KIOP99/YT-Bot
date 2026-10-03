"""
services/comment_poster.py
---------------------------
Automated comment poster for YouTube videos:
- Variable interpolation: {title}, {url}, {date}, {channel_name}
- Sequential and random template rotation from channel template pool
- Configurable delay (e.g. 1 to 5 minutes after publish)
- Exponential backoff retry logic (up to 3 attempts)
- Comment history logging in database
- Discord failure alerts
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from core.database import get_db_context
from core.logging_config import get_logger
from models.channel import YouTubeChannel
from models.comment import CommentConfig, CommentHistory, CommentTemplate
from services.discord_notify import alert_comment_failure
from services.youtube import QuotaExceededError, build_youtube_service_from_db

log = get_logger(__name__)


def format_comment_text(
    template: str,
    video_title: str,
    youtube_video_id: str,
    channel_name: str,
    pub_date: Optional[datetime] = None,
    discord_link: Optional[str] = None,
) -> str:
    """Safely replace {title}, {url}, {date}, {channel_name}, {discord_link} placeholders."""
    if not pub_date:
        pub_date = datetime.now()
    yt_url = f"https://www.youtube.com/watch?v={youtube_video_id}" if youtube_video_id else "https://youtube.com/watch?v=sample"
    date_str = pub_date.strftime("%B %d, %Y")

    text = template or ""
    text = text.replace("{title}", video_title or "")
    text = text.replace("{url}", yt_url)
    text = text.replace("{date}", date_str)
    text = text.replace("{channel_name}", channel_name or "")
    text = text.replace("{channel}", channel_name or "")
    
    disc_url = discord_link.strip() if discord_link and discord_link.strip() else ""
    text = text.replace("{discord_link}", disc_url)
    text = text.replace("{discord_server}", disc_url)
    text = text.replace("{discord}", disc_url)

    return text.strip()


async def post_auto_comment_with_retry(
    channel_id: int,
    video_id: Optional[int],
    youtube_video_id: str,
    video_title: str,
    max_retries: int = 3,
) -> bool:
    """
    Execute auto-comment workflow:
    1. Verify channel & comment config
    2. Pick next template from rotation
    3. Render variables
    4. Post comment with retry & exponential backoff
    5. Log history & alert Discord on failure
    """
    log.info(
        "Starting auto-comment process",
        channel_id=channel_id,
        yt_video_id=youtube_video_id,
        video_title=video_title,
    )

    # Clean youtube_video_id in case full URL was passed
    if "v=" in str(youtube_video_id):
        youtube_video_id = youtube_video_id.split("v=")[-1].split("&")[0]
    elif "/" in str(youtube_video_id):
        youtube_video_id = youtube_video_id.rstrip("/").split("/")[-1]

    async with get_db_context() as db:
        channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, channel_id)
        if not channel:
            log.warning("Auto-comment aborted: Channel not found", channel_id=channel_id)
            return False

        if not channel.is_authenticated and not channel.encrypted_refresh_token:
            log.warning("Auto-comment aborted: Channel not authenticated with YouTube", channel_id=channel_id)
            return False

        cfg_res = await db.execute(select(CommentConfig).where(CommentConfig.channel_id == channel_id))
        config = cfg_res.scalar_one_or_none()
        if not config or not config.is_enabled:
            log.info("Auto-comment disabled for channel, skipping", channel_id=channel_id)
            return False

        # Load active templates
        tmpl_res = await db.execute(
            select(CommentTemplate)
            .where(CommentTemplate.channel_id == channel_id, CommentTemplate.is_active == True)
            .order_by(CommentTemplate.order_index, CommentTemplate.id)
        )
        templates = tmpl_res.scalars().all()

        if not templates:
            log.info("No active templates found, using default template", channel_id=channel_id)
            from models.comment import DEFAULT_COMMENT_TEMPLATE
            raw_template = DEFAULT_COMMENT_TEMPLATE
        else:
            # Template rotation
            if config.rotation_mode == "random":
                selected_template = random.choice(templates)
            else:  # sequential
                idx = config.last_template_index % len(templates)
                selected_template = templates[idx]
                config.last_template_index = (idx + 1) % len(templates)
                await db.commit()
            raw_template = selected_template.template_text

        # Fetch channel Discord server link if configured
        from models.discord_config import DiscordConfig
        disc_res = await db.execute(select(DiscordConfig).where(DiscordConfig.channel_id == channel_id))
        disc_cfg = disc_res.scalar_one_or_none()
        discord_link = disc_cfg.button_url_2 if disc_cfg and disc_cfg.button_url_2 else ""

        formatted_comment = format_comment_text(
            raw_template,
            video_title=video_title,
            youtube_video_id=youtube_video_id,
            channel_name=channel.name,
            discord_link=discord_link,
        )

        yt_svc = build_youtube_service_from_db(channel)

    # Retry loop with backoff
    backoff_delays = [5, 15, 30]
    last_error: Optional[str] = None
    loop = asyncio.get_event_loop()

    for attempt in range(1, max_retries + 1):
        try:
            log.info(
                "Posting auto-comment to YouTube",
                channel_id=channel_id,
                yt_video_id=youtube_video_id,
                attempt=attempt,
            )
            resp = await loop.run_in_executor(
                None,
                lambda: yt_svc.post_comment(youtube_video_id, formatted_comment),
            )
            comment_id = resp.get("id", "")
            log.info(
                "Auto-comment posted successfully",
                channel_id=channel_id,
                yt_video_id=youtube_video_id,
                comment_id=comment_id,
            )

            # Record success in CommentHistory
            async with get_db_context() as db:
                history_entry = CommentHistory(
                    channel_id=channel_id,
                    video_id=video_id,
                    youtube_video_id=youtube_video_id,
                    comment_text=formatted_comment,
                    youtube_comment_id=comment_id,
                    status="success",
                    retry_count=attempt - 1,
                    posted_at=datetime.now(timezone.utc).isoformat(),
                )
                db.add(history_entry)
                await db.commit()

            return True

        except QuotaExceededError as exc:
            last_error = f"YouTube Quota Exceeded: {exc}"
            log.error("YouTube quota exceeded while auto-commenting", channel_id=channel_id, error=str(exc))
            break  # Quota exceeded cannot be retried immediately

        except Exception as exc:
            last_error = str(exc)
            log.warning(
                "Auto-comment attempt failed",
                attempt=attempt,
                max_retries=max_retries,
                error=str(exc),
            )
            if attempt < max_retries:
                delay = backoff_delays[min(attempt - 1, len(backoff_delays) - 1)]
                await asyncio.sleep(delay)

    # If all attempts failed
    error_msg = last_error or "Unknown failure during comment post"
    log.error(
        "Auto-comment permanently failed after retries",
        channel_id=channel_id,
        yt_video_id=youtube_video_id,
        error=error_msg,
    )

    # Record failure in CommentHistory
    async with get_db_context() as db:
        history_entry = CommentHistory(
            channel_id=channel_id,
            video_id=video_id,
            youtube_video_id=youtube_video_id,
            comment_text=formatted_comment,
            status="failed",
            error_message=error_msg[:1000],
            retry_count=max_retries,
            posted_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(history_entry)
        await db.commit()

    # Alert on Discord
    try:
        async with get_db_context() as db:
            channel = await db.get(YouTubeChannel, channel_id)
            channel_name = channel.name if channel else f"Channel #{channel_id}"
        await alert_comment_failure(channel_name, video_title, error_msg)
    except Exception as discord_err:
        log.warning("Could not send Discord alert for comment failure", error=str(discord_err))

    return False


async def _delayed_comment_worker(
    delay_seconds: int,
    channel_id: int,
    video_id: Optional[int],
    youtube_video_id: str,
    video_title: str,
) -> None:
    """Async background task that sleeps for delay_seconds then posts the comment."""
    log.info(
        "Auto-comment delayed execution scheduled",
        channel_id=channel_id,
        yt_video_id=youtube_video_id,
        delay_seconds=delay_seconds,
    )
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    await post_auto_comment_with_retry(channel_id, video_id, youtube_video_id, video_title)


async def trigger_auto_comment(
    channel_id: int,
    video_id: Optional[int],
    youtube_video_id: str,
    video_title: str,
) -> None:
    """
    Trigger point called when a video becomes public.
    Checks channel comment config and schedules or dispatches comment posting.
    """
    if not youtube_video_id:
        return

    clean_yt_id = str(youtube_video_id).strip()
    if "v=" in clean_yt_id:
        clean_yt_id = clean_yt_id.split("v=")[-1].split("&")[0]
    elif "/" in clean_yt_id:
        clean_yt_id = clean_yt_id.rstrip("/").split("/")[-1]

    try:
        async with get_db_context() as db:
            res = await db.execute(
                select(CommentConfig).where(CommentConfig.channel_id == channel_id)
            )
            config = res.scalar_one_or_none()

            if not config or not config.is_enabled:
                log.info(
                    "Auto-comment is not enabled for channel",
                    channel_id=channel_id,
                    yt_video_id=clean_yt_id,
                )
                return

            delay = config.delay_seconds or 0

        # Dispatch via reliable background worker with delay
        log.info(
            "Auto-comment worker dispatched",
            channel_id=channel_id,
            yt_video_id=clean_yt_id,
            delay_seconds=delay,
        )
        asyncio.create_task(
            _delayed_comment_worker(
                delay_seconds=delay,
                channel_id=channel_id,
                video_id=video_id,
                youtube_video_id=clean_yt_id,
                video_title=video_title,
            )
        )

    except Exception as exc:
        log.exception(
            "Failed to trigger auto-comment",
            channel_id=channel_id,
            yt_video_id=clean_yt_id,
            error=str(exc),
        )
