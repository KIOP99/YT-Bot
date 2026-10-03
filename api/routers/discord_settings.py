"""
api/routers/discord_settings.py
--------------------------------
Discord announcement configuration per channel.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.database import get_db
from core.security import generate_csrf_token
from models.channel import YouTubeChannel
from models.discord_config import DiscordConfig

router = APIRouter(prefix="/discord", tags=["Discord Settings"])


@router.get("", response_class=HTMLResponse)
async def discord_settings_page(
    request: Request,
    channel_id: int = 0,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channels_result = await db.execute(select(YouTubeChannel).where(YouTubeChannel.is_active == True))
    channels = channels_result.scalars().all()

    cfg = None
    if channel_id:
        result = await db.execute(
            select(DiscordConfig).where(DiscordConfig.channel_id == channel_id)
        )
        cfg = result.scalar_one_or_none()

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "discord_settings.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "selected_channel_id": channel_id,
            "config": cfg,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("/{channel_id}/config")
async def update_discord_config(
    channel_id: int,
    webhook_url: Optional[str] = Form(default=None),
    discord_channel_id: Optional[str] = Form(default=None),
    announcement_template: Optional[str] = Form(default=None),
    heading_title: Optional[str] = Form(default=None),
    custom_video_title: Optional[str] = Form(default=None),
    button_label: Optional[str] = Form(default=None),
    button_label_2: Optional[str] = Form(default=None),
    button_url_2: Optional[str] = Form(default=None),
    discord_invite_url: Optional[str] = Form(default=None),
    discord_server_url: Optional[str] = Form(default=None),
    accent_color: Optional[str] = Form(default=None),
    features_text: Optional[str] = Form(default=None),
    footer_text: Optional[str] = Form(default=None),
    include_thumbnail: bool = Form(default=True),
    use_components_v2: bool = Form(default=True),
    button_in_box: bool = Form(default=True),
    ping_role_id: Optional[str] = Form(default=None),
    is_enabled: bool = Form(default=True),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DiscordConfig).where(DiscordConfig.channel_id == channel_id)
    )
    cfg: Optional[DiscordConfig] = result.scalar_one_or_none()

    if not cfg:
        cfg = DiscordConfig(channel_id=channel_id)
        db.add(cfg)

    cfg.webhook_url = webhook_url.strip() if webhook_url and webhook_url.strip() else None
    cfg.discord_channel_id = discord_channel_id.strip() if discord_channel_id and discord_channel_id.strip() else None
    if announcement_template is not None:
        cfg.announcement_template = announcement_template
    cfg.heading_title = heading_title.strip() if heading_title else "▶️ WATCH VIDEO NOW"
    cfg.custom_video_title = custom_video_title.strip() if custom_video_title and custom_video_title.strip() else None
    cfg.button_label = button_label.strip() if button_label else "▶️ PLAY VIDEO NOW"
    cfg.button_label_2 = button_label_2.strip() if button_label_2 and button_label_2.strip() else None
    
    resolved_server_url = button_url_2 or discord_invite_url or discord_server_url
    cfg.button_url_2 = resolved_server_url.strip() if resolved_server_url and resolved_server_url.strip() else None
    cfg.accent_color = accent_color.strip() if accent_color else "#00A2C7"
    cfg.features_text = features_text if features_text else None
    cfg.footer_text = footer_text if footer_text else None
    cfg.include_thumbnail = include_thumbnail
    cfg.use_components_v2 = use_components_v2
    cfg.button_in_box = button_in_box
    cfg.ping_role_id = ping_role_id.strip() if ping_role_id and ping_role_id.strip() else None
    cfg.is_enabled = is_enabled

    await db.flush()
    return {"ok": True}


@router.post("/{channel_id}/server-link")
@router.patch("/{channel_id}/server-link")
async def edit_discord_server_link(
    channel_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Dedicated method to edit the Discord server invite link (e.g. https://discord.gg/ApzZFmQDBd)
    and button title via JSON or Form payload.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        invite_url = data.get("discord_link") or data.get("discord_invite_url") or data.get("button_url_2") or data.get("url") or ""
        label = data.get("button_label") or data.get("button_label_2") or "💬 Join Discord Server"
    else:
        form = await request.form()
        invite_url = form.get("discord_link") or form.get("discord_invite_url") or form.get("button_url_2") or form.get("url") or ""
        label = form.get("button_label") or form.get("button_label_2") or "💬 Join Discord Server"

    result = await db.execute(
        select(DiscordConfig).where(DiscordConfig.channel_id == channel_id)
    )
    cfg: Optional[DiscordConfig] = result.scalar_one_or_none()
    if not cfg:
        cfg = DiscordConfig(channel_id=channel_id)
        db.add(cfg)

    cfg.set_discord_invite(str(invite_url).strip(), str(label).strip())
    await db.flush()
    return {
        "ok": True,
        "channel_id": channel_id,
        "discord_server_url": cfg.button_url_2,
        "button_label": cfg.button_label_2,
    }


@router.post("/{channel_id}/test")
async def send_test_discord(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send a live test announcement to Discord for this channel."""
    from services.discord_notify import send_upload_announcement
    ch = await db.get(YouTubeChannel, channel_id)
    ch_name = ch.name if ch else "YouTube Channel"

    msg_id = await send_upload_announcement(
        channel_id=channel_id,
        video_title="FREE PANEL PC OB55",
        youtube_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        channel_name=ch_name,
    )

    if msg_id:
        return {"ok": True, "message_id": msg_id, "message": "Test announcement sent to Discord successfully!"}
    else:
        raise HTTPException(
            status_code=400,
            detail="Failed to send Discord announcement. Please check your Discord Channel ID or Webhook URL in settings.",
        )


@router.get("/{channel_id}/export")
async def export_discord_config(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export the channel's Discord configuration as JSON."""
    result = await db.execute(
        select(DiscordConfig).where(DiscordConfig.channel_id == channel_id)
    )
    cfg: Optional[DiscordConfig] = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")

    data = {
        "webhook_url": cfg.webhook_url or "",
        "discord_channel_id": cfg.discord_channel_id or "",
        "ping_role_id": cfg.ping_role_id or "",
        "heading_title": cfg.heading_title or "▶️ WATCH VIDEO NOW",
        "button_label": cfg.button_label or "▶️ PLAY VIDEO NOW",
        "button_label_2": cfg.button_label_2 or "",
        "button_url_2": cfg.button_url_2 or "",
        "accent_color": cfg.accent_color or "#00A2C7",
        "features_text": cfg.features_text or "",
        "footer_text": cfg.footer_text or "",
        "include_thumbnail": cfg.include_thumbnail,
        "use_components_v2": cfg.use_components_v2,
        "button_in_box": cfg.button_in_box,
        "is_enabled": cfg.is_enabled,
    }
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f"attachment; filename=discord_config_channel_{channel_id}.json"}
    )


@router.post("/{channel_id}/preview")
async def preview_announcement(
    channel_id: int,
    template: Optional[str] = Form(default=None),
    heading_title: Optional[str] = Form(default="▶️ WATCH VIDEO NOW"),
    features_text: Optional[str] = Form(default=""),
    footer_text: Optional[str] = Form(default=""),
    accent_color: Optional[str] = Form(default="#00A2C7"),
    button_label: Optional[str] = Form(default="▶️ PLAY VIDEO NOW"),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render a preview payload with sample values."""
    from services.discord_notify import (
        build_components_v2_payload,
        parse_accent_color,
    )
    ch = await db.get(YouTubeChannel, channel_id)
    ch_name = ch.name if ch else "NINJA HEX"

    accent_int = parse_accent_color(accent_color)
    buttons = [(button_label or "▶️ PLAY VIDEO NOW", "https://www.youtube.com")]

    payload = build_components_v2_payload(
        channel_name=ch_name,
        heading_title=heading_title or "▶️ WATCH VIDEO NOW",
        video_title="FREE PANEL PC OB55",
        features_text=features_text,
        footer_text=footer_text,
        buttons_data=buttons,
        thumbnail_url="https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800&auto=format&fit=crop",
        accent_color=accent_int,
        ping_mention="@everyone",
        time_str="Today at 08:57",
        button_in_box=True,
    )
    return {"ok": True, "payload": payload}
