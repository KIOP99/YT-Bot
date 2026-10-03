"""
api/routers/comments.py
-------------------------
Auto-comment configuration, template pool management, live preview, and testing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import require_admin
from api.templates import templates
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token
from models.channel import YouTubeChannel
from models.comment import (
    DEFAULT_COMMENT_TEMPLATE,
    CommentConfig,
    CommentHistory,
    CommentTemplate,
)
from models.discord_config import DiscordConfig
from services.comment_poster import format_comment_text
from services.youtube import QuotaExceededError, build_youtube_service_from_db

log = get_logger(__name__)

router = APIRouter(prefix="/comments", tags=["Auto Comment"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def comments_page(
    request: Request,
    channel_id: int = 0,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the Auto Comment dashboard page."""
    channels_res = await db.execute(
        select(YouTubeChannel).where(YouTubeChannel.is_active == True).order_by(YouTubeChannel.name)
    )
    channels = channels_res.scalars().all()

    if channel_id == 0 and channels:
        channel_id = channels[0].id

    cfg = None
    channel_templates = []
    history_items = []

    if channel_id:
        cfg_res = await db.execute(
            select(CommentConfig).where(CommentConfig.channel_id == channel_id)
        )
        cfg = cfg_res.scalar_one_or_none()

        # If channel has no config yet, create default config & initial template
        if not cfg:
            cfg = CommentConfig(
                channel_id=channel_id,
                is_enabled=False,
                rotation_mode="sequential",
                delay_seconds=60,
                last_template_index=0,
            )
            db.add(cfg)
            await db.flush()

            initial_tmpl = CommentTemplate(
                config_id=cfg.id,
                channel_id=channel_id,
                template_text=DEFAULT_COMMENT_TEMPLATE,
                is_active=True,
                order_index=0,
            )
            db.add(initial_tmpl)
            await db.commit()
            await db.refresh(cfg)

        tmpl_res = await db.execute(
            select(CommentTemplate)
            .where(CommentTemplate.channel_id == channel_id)
            .order_by(CommentTemplate.order_index, CommentTemplate.id)
        )
        channel_templates = tmpl_res.scalars().all()

        hist_res = await db.execute(
            select(CommentHistory)
            .where(CommentHistory.channel_id == channel_id)
            .order_by(desc(CommentHistory.created_at))
            .limit(25)
        )
        history_items = hist_res.scalars().all()

        disc_res = await db.execute(select(DiscordConfig).where(DiscordConfig.channel_id == channel_id))
        disc_cfg = disc_res.scalar_one_or_none()
        discord_server_url = disc_cfg.button_url_2 if disc_cfg and disc_cfg.button_url_2 else ""
    else:
        discord_server_url = ""

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "comments.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "selected_channel_id": channel_id,
            "config": cfg,
            "templates": channel_templates,
            "history": history_items,
            "discord_server_url": discord_server_url,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("/{channel_id}/config")
async def update_comment_config(
    channel_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Save auto-comment channel settings (toggle, rotation mode, delay)."""
    # Accept both JSON and Form data
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        is_enabled = bool(data.get("is_enabled", False))
        rotation_mode = str(data.get("rotation_mode", "sequential"))
        delay_seconds = int(data.get("delay_seconds", 60))
    else:
        form = await request.form()
        is_enabled = form.get("is_enabled") in ("true", "True", "on", "1", True)
        rotation_mode = str(form.get("rotation_mode", "sequential"))
        try:
            delay_seconds = int(form.get("delay_seconds", 60))
        except (ValueError, TypeError):
            delay_seconds = 60

    if rotation_mode not in ("sequential", "random"):
        rotation_mode = "sequential"

    if delay_seconds < 0:
        delay_seconds = 0
    elif delay_seconds > 1800:  # Max 30 minutes
        delay_seconds = 1800

    cfg_res = await db.execute(
        select(CommentConfig).where(CommentConfig.channel_id == channel_id)
    )
    cfg = cfg_res.scalar_one_or_none()

    if not cfg:
        cfg = CommentConfig(channel_id=channel_id)
        db.add(cfg)

    cfg.is_enabled = is_enabled
    cfg.rotation_mode = rotation_mode
    cfg.delay_seconds = delay_seconds

    await db.commit()
    log.info(
        "Updated comment config",
        channel_id=channel_id,
        is_enabled=is_enabled,
        rotation_mode=rotation_mode,
        delay_seconds=delay_seconds,
    )
    return {"ok": True, "message": "Settings saved successfully"}


@router.post("/{channel_id}/templates")
async def create_template(
    channel_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new template to the channel's pool."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        template_text = str(data.get("template_text", "")).strip()
        is_active = bool(data.get("is_active", True))
    else:
        form = await request.form()
        template_text = str(form.get("template_text", "")).strip()
        is_active = form.get("is_active") in ("true", "True", "on", "1", True)

    if not template_text:
        raise HTTPException(status_code=400, detail="Template text cannot be empty.")

    cfg_res = await db.execute(
        select(CommentConfig).where(CommentConfig.channel_id == channel_id)
    )
    cfg = cfg_res.scalar_one_or_none()
    if not cfg:
        cfg = CommentConfig(channel_id=channel_id)
        db.add(cfg)
        await db.flush()

    # Get max order index
    count_res = await db.execute(
        select(CommentTemplate).where(CommentTemplate.channel_id == channel_id)
    )
    current_count = len(count_res.scalars().all())

    template = CommentTemplate(
        config_id=cfg.id,
        channel_id=channel_id,
        template_text=template_text,
        is_active=is_active,
        order_index=current_count,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    return {
        "ok": True,
        "id": template.id,
        "template_text": template.template_text,
        "is_active": template.is_active,
        "order_index": template.order_index,
    }


@router.put("/{channel_id}/templates/{template_id}")
async def update_template(
    channel_id: int,
    template_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing template's text or active status."""
    tmpl: Optional[CommentTemplate] = await db.get(CommentTemplate, template_id)
    if not tmpl or tmpl.channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Template not found")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        if "template_text" in data:
            text = str(data["template_text"]).strip()
            if not text:
                raise HTTPException(status_code=400, detail="Template text cannot be empty")
            tmpl.template_text = text
        if "is_active" in data:
            tmpl.is_active = bool(data["is_active"])
        if "order_index" in data:
            tmpl.order_index = int(data["order_index"])
    else:
        form = await request.form()
        if "template_text" in form:
            text = str(form.get("template_text", "")).strip()
            if not text:
                raise HTTPException(status_code=400, detail="Template text cannot be empty")
            tmpl.template_text = text
        if "is_active" in form:
            tmpl.is_active = form.get("is_active") in ("true", "True", "on", "1", True)

    await db.commit()
    return {"ok": True, "message": "Template updated"}


@router.delete("/{channel_id}/templates/{template_id}")
async def delete_template(
    channel_id: int,
    template_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a template from the pool."""
    tmpl: Optional[CommentTemplate] = await db.get(CommentTemplate, template_id)
    if not tmpl or tmpl.channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Template not found")

    await db.delete(tmpl)
    await db.commit()
    return {"ok": True, "message": "Template deleted"}


@router.post("/{channel_id}/preview")
async def preview_comment(
    channel_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render a live preview of the comment with sample or real channel data."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        raw_text = str(data.get("template_text", ""))
        sample_title = str(data.get("title", "EPIC Free Fire Highlights #42"))
    else:
        form = await request.form()
        raw_text = str(form.get("template_text", ""))
        sample_title = str(form.get("title", "EPIC Free Fire Highlights #42"))

    channel = await db.get(YouTubeChannel, channel_id)
    ch_name = channel.name if channel else "My Channel"

    disc_res = await db.execute(select(DiscordConfig).where(DiscordConfig.channel_id == channel_id))
    disc_cfg = disc_res.scalar_one_or_none()
    discord_link = disc_cfg.button_url_2 if disc_cfg and disc_cfg.button_url_2 else None

    rendered = format_comment_text(
        template=raw_text,
        video_title=sample_title,
        youtube_video_id="dQw4w9WgXcQ",
        channel_name=ch_name,
        pub_date=datetime.now(),
        discord_link=discord_link,
    )
    return {"ok": True, "preview": rendered}


@router.post("/{channel_id}/test")
async def send_test_comment(
    channel_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually post a test comment to a specific YouTube video ID.
    Enables instant verification that OAuth force-ssl permissions and formatting work!
    """
    channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel.is_authenticated:
        raise HTTPException(
            status_code=400,
            detail=f"Channel '{channel.name}' is not authenticated with YouTube yet. Please connect OAuth first.",
        )

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        yt_video_id = str(data.get("youtube_video_id", "")).strip()
        custom_text = str(data.get("template_text", "")).strip()
    else:
        form = await request.form()
        yt_video_id = str(form.get("youtube_video_id", "")).strip()
        custom_text = str(form.get("template_text", "")).strip()

    if not yt_video_id:
        raise HTTPException(status_code=400, detail="YouTube Video ID is required.")

    disc_res = await db.execute(select(DiscordConfig).where(DiscordConfig.channel_id == channel_id))
    disc_cfg = disc_res.scalar_one_or_none()
    discord_link = disc_cfg.button_url_2 if disc_cfg and disc_cfg.button_url_2 else None

    # Format text
    text_to_post = custom_text or DEFAULT_COMMENT_TEMPLATE
    comment_body = format_comment_text(
        template=text_to_post,
        video_title="Test Video",
        youtube_video_id=yt_video_id,
        channel_name=channel.name,
        discord_link=discord_link,
    )

    yt_svc = build_youtube_service_from_db(channel)
    loop = asyncio.get_event_loop()

    try:
        resp = await loop.run_in_executor(
            None,
            lambda: yt_svc.post_comment(yt_video_id, comment_body),
        )
        comment_id = resp.get("id", "")

        # Record in history
        history_entry = CommentHistory(
            channel_id=channel_id,
            youtube_video_id=yt_video_id,
            comment_text=comment_body,
            youtube_comment_id=comment_id,
            status="success",
            posted_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(history_entry)
        await db.commit()

        return {
            "ok": True,
            "message": "Test comment posted successfully to YouTube!",
            "comment_id": comment_id,
            "comment_text": comment_body,
        }

    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=f"YouTube API Quota Exceeded: {e}")
    except Exception as e:
        log.exception("Test comment posting failed", error=str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Failed to post comment to YouTube: {e}. Note: If scope error, re-authenticate channel with YouTube to grant comment permissions.",
        )
