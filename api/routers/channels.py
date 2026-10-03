"""
api/routers/channels.py
------------------------
YouTube channel management: CRUD, OAuth flow initiation and callback.
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token
from models.channel import YouTubeChannel
from models.discord_config import DiscordConfig
from models.discord_bot import DiscordBot
from models.schedule_config import ScheduleConfig
from services.encryption import encrypt
from services.youtube import YouTubeService

log = get_logger(__name__)
router = APIRouter(prefix="/channels", tags=["Channels"])


@router.get("", response_class=HTMLResponse)
async def channels_page(
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(YouTubeChannel).order_by(YouTubeChannel.id))
    channels = result.scalars().all()
    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "channels.html",
        {"request": request, "user": user, "channels": channels, "csrf_token": csrf},
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("")
async def create_channel(
    name: str = Form(...),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channel = YouTubeChannel(name=name)
    db.add(channel)
    await db.flush()

    # Create default schedule, discord config, and discord bot
    db.add(ScheduleConfig(channel_id=channel.id))
    db.add(DiscordConfig(channel_id=channel.id))
    db.add(DiscordBot(channel_id=channel.id))
    await db.commit()

    return RedirectResponse(url="/api/channels", status_code=303)


@router.get("/{channel_id}", response_class=HTMLResponse)
async def channel_detail(
    request: Request,
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "channels.html",
        {"request": request, "user": user, "channel": channel, "csrf_token": csrf},
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    await db.delete(channel)
    await db.commit()
    return {"ok": True}


# ── OAuth Flow ─────────────────────────────────────────────────────────────

@router.get("/{channel_id}/oauth")
async def initiate_oauth(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Redirect admin to Google OAuth consent screen."""
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    state = f"{channel_id}:{secrets.token_urlsafe(16)}"
    channel.oauth_state = state
    await db.commit()

    auth_url = YouTubeService.get_authorization_url(state)
    return RedirectResponse(url=auth_url)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle Google OAuth callback and store encrypted tokens."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    # Validate state
    try:
        channel_id_str, _ = state.split(":", 1)
        channel_id = int(channel_id_str)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    channel = await db.get(YouTubeChannel, channel_id)
    if not channel or channel.oauth_state != state:
        raise HTTPException(status_code=400, detail="State mismatch — possible CSRF")

    # Exchange code for tokens
    tokens = YouTubeService.exchange_code(code)

    # Store encrypted tokens
    channel.encrypted_access_token = encrypt(tokens["access_token"])
    channel.encrypted_refresh_token = encrypt(tokens["refresh_token"])
    channel.token_expiry = tokens["token_expiry"]
    channel.oauth_state = None
    channel.is_authenticated = True

    # Fetch channel info from YouTube
    svc = YouTubeService(
        channel_id=channel_id,
        encrypted_access_token=channel.encrypted_access_token,
        encrypted_refresh_token=channel.encrypted_refresh_token,
        token_expiry=channel.token_expiry,
    )
    try:
        info = svc.get_channel_info()
        channel.youtube_channel_id = info["youtube_channel_id"]
        channel.name = info["title"]
        channel.thumbnail_url = info.get("thumbnail_url")
        channel.subscriber_count = info.get("subscriber_count", 0)
        channel.custom_url = info.get("custom_url")
    except Exception as exc:
        log.warning("Could not fetch channel info", error=str(exc))

    await db.commit()
    log.info("OAuth complete", channel_id=channel_id, yt_channel=channel.youtube_channel_id)
    return RedirectResponse(url=f"/api/channels?oauth=success&id={channel_id}", status_code=302)


@router.post("/{channel_id}/sync")
async def sync_channel(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Refresh channel title, avatar, and stats directly from YouTube API."""
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel or not channel.is_authenticated:
        raise HTTPException(status_code=400, detail="Channel is not authenticated with YouTube")

    svc = YouTubeService(
        channel_id=channel_id,
        encrypted_access_token=channel.encrypted_access_token,
        encrypted_refresh_token=channel.encrypted_refresh_token,
        token_expiry=channel.token_expiry,
    )
    try:
        info = svc.get_channel_info()
        channel.youtube_channel_id = info["youtube_channel_id"]
        channel.name = info["title"]
        channel.thumbnail_url = info.get("thumbnail_url")
        channel.subscriber_count = info.get("subscriber_count", 0)
        channel.custom_url = info.get("custom_url")
        await db.commit()
        return {"ok": True, "channel": {"name": channel.name, "thumbnail_url": channel.thumbnail_url, "subscriber_count": channel.subscriber_count}}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch YouTube info: {exc}")
