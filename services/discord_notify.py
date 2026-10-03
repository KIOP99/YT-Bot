"""
services/discord_notify.py
---------------------------
Admin DM alerts only — upload announcements fully removed.
"""

from __future__ import annotations

from typing import Optional

import discord

from core.config import settings
from core.logging_config import get_logger

log = get_logger(__name__)

_bot_instance: Optional[discord.Client] = None


def set_bot(bot: discord.Client) -> None:
    global _bot_instance
    _bot_instance = bot


def get_bot() -> Optional[discord.Client]:
    return _bot_instance

async def _get_bot_token_for_channel(channel_id: int) -> str:
    """
    Returns the Discord bot token for the given YouTube channel_id.
    Checks the discord_bots DB table first; falls back to the global .env token.
    """
    try:
        from core.database import get_db_context
        from models.discord_bot import DiscordBot
        from sqlalchemy import select

        async with get_db_context() as db:
            res = await db.execute(
                select(DiscordBot).where(
                    DiscordBot.channel_id == channel_id,
                    DiscordBot.is_enabled == True,
                )
            )
            db_bot = res.scalar_one_or_none()
            if db_bot and db_bot.bot_token and db_bot.bot_token.strip():
                return db_bot.bot_token.strip()
    except Exception:
        pass
    # Fallback to global .env token
    return settings.discord_bot_token or ""


async def send_admin_dm(message: str) -> None:
    """Send a direct message to the configured admin user."""
    bot = get_bot()
    if not bot:
        return
    try:
        user = await bot.fetch_user(settings.discord_admin_user_id)
        await user.send(message)
        log.info("Admin DM sent", user_id=settings.discord_admin_user_id)
    except Exception as exc:
        log.exception("Failed to send admin DM", error=str(exc))


async def alert_thumbnail_pool_empty(channel_name: str) -> None:
    await send_admin_dm(
        f"⚠️ **YTBot Alert**: Thumbnail pool for **{channel_name}** is empty! "
        "Add thumbnails via the dashboard before the next upload."
    )


async def alert_upload_failure(channel_name: str, error: str, retry: int) -> None:
    await send_admin_dm(
        f"❌ **YTBot Upload Failed** — Channel: **{channel_name}**\n"
        f"Retry #{retry} | Error: ```{error[:500]}```"
    )


async def alert_upload_limit_exceeded(channel_name: str) -> None:
    """Send an actionable alert when a channel hits YouTube's daily upload limit."""
    await send_admin_dm(
        f"🚫 **YTBot Alert: Daily YouTube Upload Limit Reached** — Channel: **{channel_name}**\n\n"
        f"YouTube has capped this channel's video uploads for today (Google 24-hour channel limit).\n\n"
        f"**How to resolve in hosting:**\n"
        f"1️⃣ **Enable Advanced Features (Instant Fix)**: Go to **YouTube Studio** > **Settings** > **Channel** > **Feature eligibility** and enable **Advanced features** (via 30s Video Verification or ID) to unlock 100+ daily uploads.\n"
        f"2️⃣ **Switch YouTube Channel**: In your YTBot dashboard **Channels** tab, switch to another connected channel to upload right away.\n"
        f"3️⃣ **Wait 24 Hours**: Google's rolling 24-hour upload quota will reset automatically."
    )


async def alert_comment_failure(channel_name: str, video_title: str, error: str) -> None:
    await send_admin_dm(
        f"❌ **YTBot Auto-Comment Failed** — Channel: **{channel_name}**\n"
        f"Video: **{video_title}**\n"
        f"Error: ```{error[:500]}```"
    )


async def alert_new_credentials(username: str, password: str) -> None:
    await send_admin_dm(
        "🔐 **Dashboard Credentials Rotated**\n\n"
        f"**Username:** `{username}`\n"
        f"**Password:** `{password}`\n\n"
        "_Valid until next rotation. Do not share._"
    )


COMPONENTS_V2_FLAG = 1 << 15
DEFAULT_ACCENT_COLOR = 0x00A2C7


def resolve_direct_image_url(url: str) -> str:
    """
    Resolves webpage/share URLs (Tenor, Giphy, Imgur) to direct image / GIF URLs.
    Ensures Discord Webhooks and Components V2 render animated GIFs, WEBP, PNG, JPG properly.
    """
    import re
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return url

    # 1. Tenor view link
    tenor_match = re.search(r'tenor\.com/view/[^/]*?-(\d+)', url) or re.search(r'tenor\.com/view/.*?(\d+)$', url)
    if tenor_match:
        gif_id = tenor_match.group(1)
        return f"https://media.tenor.com/{gif_id}/tenor.gif"

    # 2. Giphy view link
    giphy_match = re.search(r'giphy\.com/gifs/(?:.*?-)?([a-zA-Z0-9]+)$', url)
    if giphy_match:
        gif_id = giphy_match.group(1)
        return f"https://media.giphy.com/media/{gif_id}/giphy.gif"

    # 3. Imgur view link
    imgur_match = re.search(r'imgur\.com/(?:gallery/)?([a-zA-Z0-9]+)$', url)
    if imgur_match and not url.lower().endswith(('.gif', '.png', '.jpg', '.jpeg', '.webp', '.ico')):
        img_id = imgur_match.group(1)
        return f"https://i.imgur.com/{img_id}.gif"

    return url


def extract_youtube_video_id(url: str) -> Optional[str]:
    """Extract YouTube 11-char video ID from url."""
    import re
    if not url:
        return None
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11})",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"youtube\.com\/shorts\/([0-9A-Za-z_-]{11})"
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def parse_accent_color(color_str: Optional[str]) -> int:
    """Parse hex string (#00A2C7 or 00A2C7) to integer."""
    if not color_str:
        return DEFAULT_ACCENT_COLOR
    clean = color_str.strip().lstrip("#")
    try:
        return int(clean, 16)
    except Exception:
        return DEFAULT_ACCENT_COLOR


def build_components_v2_payload(
    channel_name: str,
    heading_title: str,
    video_title: str,
    features_text: Optional[str],
    footer_text: Optional[str],
    buttons_data: list[tuple[str, str]],
    thumbnail_url: str = "",
    accent_color: int = DEFAULT_ACCENT_COLOR,
    ping_mention: str = "",
    time_str: str = "",
    button_in_box: bool = True,
) -> dict:
    """
    Builds a Discord Components V2 payload matching the user design:
    - Container (type 17) with accent color
    - Channel name header
    - Big heading title (# WATCH VIDEO NOW)
    - Divider lines (type 14)
    - Video Title & Features bullet points
    - Giveaway / CTA footer
    - Media Gallery (type 12) for thumbnail preview
    - Subtext timestamp footer
    - ActionRow (type 1) with Link buttons (type 2, style 5) inside the container box!
    """
    container_children = []

    # 1. Top channel banner line
    ch_display = channel_name.strip() if channel_name else "YouTube Channel"
    container_children.append({
        "type": 10,
        "content": f"**{ch_display}** published a new video"
    })

    # 2. Large Heading
    h_title = (heading_title or "▶️ WATCH VIDEO NOW").strip()
    if not h_title.startswith("#"):
        h_title = f"# {h_title}"
    container_children.append({
        "type": 10,
        "content": h_title
    })

    # 3. Divider
    container_children.append({"type": 14, "divider": True, "spacing": 1})

    # 4. Video Title & Features Bullet Points
    body_parts = []
    if video_title:
        body_parts.append(f"**{video_title.strip()}**")

    if features_text and features_text.strip():
        lines = [line.strip() for line in features_text.strip().split("\n") if line.strip()]
        formatted_lines = []
        for line in lines:
            if not line.startswith(("•", "-", "*")):
                formatted_lines.append(f"• {line}")
            else:
                formatted_lines.append(line)
        body_parts.append("\n".join(formatted_lines))

    if body_parts:
        container_children.append({
            "type": 10,
            "content": "\n\n".join(body_parts)
        })

    # 5. Giveaway / Comment text (Divider + Footer CTA)
    if footer_text and footer_text.strip():
        container_children.append({"type": 14, "divider": True, "spacing": 1})
        container_children.append({
            "type": 10,
            "content": footer_text.strip()
        })

    # 6. Media Gallery / Thumbnail Preview (type 12)
    resolved_thumb = resolve_direct_image_url(thumbnail_url)
    if resolved_thumb and (resolved_thumb.startswith("http://") or resolved_thumb.startswith("https://")):
        container_children.append({
            "type": 12,
            "items": [
                {
                    "media": {
                        "url": resolved_thumb
                    }
                }
            ]
        })

    # 7. Subtext timestamp footer
    t_text = time_str or "Today"
    container_children.append({
        "type": 10,
        "content": f"-# ▶️ YOUTUBE {ch_display} • {t_text}"
    })

    # 8. Action Row containing Link buttons placed side-by-side
    action_row_components = []
    for btn_label, btn_url in buttons_data:
        if btn_label and btn_url:
            action_row_components.append({
                "type": 2,   # Button
                "style": 5,  # Link button (opens URL)
                "label": btn_label.strip(),
                "url": btn_url.strip()
            })

    top_level_components = []

    # Optional top-level ping (@everyone or role)
    if ping_mention and ping_mention.strip():
        top_level_components.append({
            "type": 10,
            "content": ping_mention.strip()
        })

    if button_in_box and action_row_components:
        # Button inside the container box!
        container_children.append({
            "type": 1,
            "components": action_row_components
        })

    container = {
        "type": 17,  # Container
        "accent_color": accent_color,
        "components": container_children
    }
    top_level_components.append(container)

    if not button_in_box and action_row_components:
        # Button placed outside container
        top_level_components.append({
            "type": 1,
            "components": action_row_components
        })

    return {
        "flags": COMPONENTS_V2_FLAG,
        "components": top_level_components
    }


# Deduplication cache to prevent sending duplicate Discord announcements within 60 seconds
_recent_announcements: dict[str, tuple[float, int]] = {}


async def send_upload_announcement(
    channel_id: int,
    video_title: str,
    youtube_url: str,
    thumbnail_path: Optional[str] = None,
    channel_name: str = "",
) -> Optional[int]:
    """
    Send a video release announcement to the configured Discord webhook or channel.
    Uses Components V2 (Container + Button inside box) with fallback to rich Embed + ActionRow.
    Returns the Discord message ID if sent successfully, or None.
    Guaranteed deduplication: Suppresses duplicate announcements for the same video within 60s.
    """
    import os
    import time
    from datetime import datetime
    import httpx
    from core.database import get_db_context
    from models.discord_config import DiscordConfig
    from sqlalchemy import select

    # ── Deduplication Check ──────────────────────────────────────────────────
    now_ts = time.time()
    dedup_key = f"{channel_id}:{youtube_url.strip()}"
    if dedup_key in _recent_announcements:
        prev_ts, prev_msg_id = _recent_announcements[dedup_key]
        if now_ts - prev_ts < 60.0:
            log.warning(
                "Duplicate Discord announcement suppressed within 60s cooldown window",
                channel_id=channel_id,
                youtube_url=youtube_url,
                seconds_ago=round(now_ts - prev_ts, 1),
                prev_message_id=prev_msg_id,
            )
            return prev_msg_id

    # Clean old cache entries (> 300s)
    for k in list(_recent_announcements.keys()):
        if now_ts - _recent_announcements[k][0] > 300.0:
            _recent_announcements.pop(k, None)

    async with get_db_context() as db:
        res = await db.execute(
            select(DiscordConfig).where(DiscordConfig.channel_id == channel_id)
        )
        cfg: Optional[DiscordConfig] = res.scalar_one_or_none()
        if not cfg or not cfg.is_enabled:
            log.info("Discord announcement not enabled for channel", channel_id=channel_id)
            return None

        webhook_url = cfg.webhook_url.strip() if cfg.webhook_url else None
        discord_channel_id = cfg.discord_channel_id.strip() if cfg.discord_channel_id else None

        if not webhook_url and not discord_channel_id:
            log.info("Neither webhook_url nor discord_channel_id configured", channel_id=channel_id)
            return None

        ping_role_id = cfg.ping_role_id.strip() if cfg.ping_role_id else None
        heading_title = cfg.heading_title or "▶️ WATCH VIDEO NOW"
        button_label = cfg.button_label or "▶️ PLAY VIDEO NOW"
        button_label_2 = cfg.button_label_2
        button_url_2 = cfg.button_url_2
        accent_color_int = parse_accent_color(cfg.accent_color)
        features_text = cfg.features_text
        footer_text = cfg.footer_text
        use_components_v2 = cfg.use_components_v2 if cfg.use_components_v2 is not None else True
        button_in_box = cfg.button_in_box if cfg.button_in_box is not None else True
        include_thumbnail = cfg.include_thumbnail if cfg.include_thumbnail is not None else True
        custom_video_title = getattr(cfg, "custom_video_title", None)

    # Resolve Video Title
    if custom_video_title:
        video_title = custom_video_title.replace("{title}", video_title)

    # Resolve Ping Mention
    ping_mention = ""
    if ping_role_id:
        if ping_role_id.lower() in ["@everyone", "everyone"]:
            ping_mention = "@everyone"
        elif ping_role_id.lower() in ["@here", "here"]:
            ping_mention = "@here"
        else:
            ping_mention = f"<@&{ping_role_id}>"

    # Resolve Buttons Data
    buttons_data = []
    if button_label and youtube_url:
        buttons_data.append((button_label, youtube_url))
    if button_label_2 and button_url_2:
        buttons_data.append((button_label_2, button_url_2))

    # Resolve Thumbnail Image URL
    thumb_url = ""
    if include_thumbnail:
        yt_id = extract_youtube_video_id(youtube_url)
        if yt_id:
            thumb_url = f"https://i.ytimg.com/vi/{yt_id}/maxresdefault.jpg"

    now_time = datetime.now().strftime("Today at %H:%M")

    # 1. Attempt sending with Discord Components V2
    if use_components_v2:
        payload = build_components_v2_payload(
            channel_name=channel_name or "YouTube Channel",
            heading_title=heading_title,
            video_title=video_title,
            features_text=features_text,
            footer_text=footer_text,
            buttons_data=buttons_data,
            thumbnail_url=thumb_url,
            accent_color=accent_color_int,
            ping_mention=ping_mention,
            time_str=now_time,
            button_in_box=button_in_box,
        )

        # 1a. If Webhook URL is configured
        if webhook_url:
            try:
                target_url = webhook_url
                if "?" not in target_url:
                    target_url += "?with_components=true"
                elif "with_components" not in target_url:
                    target_url += "&with_components=true"

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(target_url, json=payload)
                    if resp.status_code in [200, 204]:
                        log.info("Components V2 announcement sent via Webhook successfully!", channel_id=channel_id)
                        try:
                            data = resp.json()
                            msg_id = int(data.get("id", 1))
                        except Exception:
                            msg_id = 1
                        _recent_announcements[dedup_key] = (time.time(), msg_id)
                        return msg_id
                    else:
                        log.warning("Webhook Components V2 returned error", status=resp.status_code, body=resp.text[:300])
            except Exception as e:
                log.exception("Error posting Components V2 to webhook", error=str(e))

        # 1b. If Discord Bot Channel ID is configured
        if discord_channel_id:
            try:
                # Prefer per-channel bot token from DB; fall back to global .env token
                bot_token = await _get_bot_token_for_channel(channel_id)
                if not bot_token:
                    log.warning("No bot token found for channel, skipping REST API post", channel_id=channel_id)
                else:
                    api_url = f"https://discord.com/api/v10/channels/{discord_channel_id}/messages"
                    headers = {
                        "Authorization": f"Bot {bot_token}",
                        "Content-Type": "application/json"
                    }
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(api_url, headers=headers, json=payload)
                        if resp.status_code in [200, 201]:
                            data = resp.json()
                            msg_id = int(data.get("id"))
                            log.info("Components V2 announcement sent via Bot REST API", message_id=msg_id)
                            _recent_announcements[dedup_key] = (time.time(), msg_id)
                            return msg_id
                        else:
                            log.warning("Discord REST API Components V2 failed", status=resp.status_code, body=resp.text[:300])
            except Exception as e:
                log.exception("Error posting Components V2 via Discord REST API", error=str(e))

    # 2. Fallback to Discord Embed + ActionRow View
    log.info("Falling back to standard Discord Embed announcement")
    # Use per-channel bot from manager if available, else fall back to legacy singleton
    from bot.manager import bot_manager
    bot = bot_manager.get_bot(channel_id)
    if bot is None:
        from services.discord_notify import get_bot as _get_legacy_bot
        bot = _get_legacy_bot()
    if not bot or not discord_channel_id:
        log.warning("Cannot use Embed fallback: bot or channel ID missing")
        return None

    try:
        target_ch_id = int(discord_channel_id)
        discord_ch = bot.get_channel(target_ch_id)
        if not discord_ch:
            discord_ch = await bot.fetch_channel(target_ch_id)
        if not discord_ch:
            log.warning("Discord channel not found for embed fallback", channel_id=target_ch_id)
            return None

        # Build Rich Embed
        embed = discord.Embed(
            title=heading_title or "▶️ WATCH VIDEO NOW",
            color=accent_color_int
        )
        embed.set_author(name=f"{channel_name or 'YouTube Channel'} published a new video")
        
        desc_lines = ["──────────────────────────────"]
        if video_title:
            desc_lines.append(f"**{video_title}**")
        if features_text and features_text.strip():
            desc_lines.append(features_text.strip())
        desc_lines.append("──────────────────────────────")
        if footer_text and footer_text.strip():
            desc_lines.append(footer_text.strip())
        embed.description = "\n".join(desc_lines)

        file = None
        if include_thumbnail:
            if thumb_url:
                embed.set_image(url=thumb_url)
            elif thumbnail_path and os.path.exists(thumbnail_path):
                file = discord.File(thumbnail_path, filename="thumbnail.jpg")
                embed.set_image(url="attachment://thumbnail.jpg")

        embed.set_footer(text=f"▶️ YOUTUBE {channel_name or 'YouTube Channel'} • {now_time}")

        # ActionRow View with link buttons
        view = None
        if buttons_data:
            view = discord.ui.View()
            for b_lbl, b_url in buttons_data:
                view.add_item(discord.ui.Button(label=b_lbl, url=b_url, style=discord.ButtonStyle.link))

        msg = await discord_ch.send(
            content=ping_mention or None,
            embed=embed,
            file=file,
            view=view
        )
        log.info("Discord Embed fallback announcement sent successfully", message_id=msg.id)
        _recent_announcements[dedup_key] = (time.time(), msg.id)
        return msg.id

    except Exception as e:
        log.exception("Failed to send Discord announcement via fallback", error=str(e))
        return None


