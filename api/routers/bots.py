"""
api/routers/bots.py
--------------------
Multi-bot management endpoints.
Allows creating, updating, deleting, and controlling per-channel Discord bots
through the web dashboard.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.dependencies import require_admin
from api.templates import templates
from core.database import get_db
from core.security import generate_csrf_token
from models.channel import YouTubeChannel
from models.discord_bot import DiscordBot

router = APIRouter(prefix="/bots", tags=["Discord Bots"])

ASSETS_DIR = Path("uploads/bot_assets")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────

def _allowed_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}


async def _save_upload(file: UploadFile, sub: str) -> str:
    """Save an uploaded file and return its relative path."""
    dest_dir = ASSETS_DIR / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return str(dest)


# ── Pages ──────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def bots_page(
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channels_result = await db.execute(
        select(YouTubeChannel)
        .where(YouTubeChannel.is_active == True)
        .options(selectinload(YouTubeChannel.discord_bot))
    )
    channels = channels_result.scalars().all()

    bots_result = await db.execute(select(DiscordBot))
    bots = {b.channel_id: b for b in bots_result.scalars().all()}

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "bot_settings.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "bots": bots,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


# ── API ─────────────────────────────────────────────────────────────────────

@router.get("/status", response_class=JSONResponse)
async def bots_status(
    user=Depends(require_admin),
):
    """Return running status of all bots."""
    from bot.manager import bot_manager

    statuses = {}
    for ch_id, bot in bot_manager.all_bots().items():
        statuses[ch_id] = {
            "running": not bot.is_closed(),
            "user": str(bot.user) if bot.user else None,
            "guilds": len(bot.guilds),
        }
    return {"bots": statuses}


@router.post("/{channel_id}/save")
async def save_bot_config(
    channel_id: int,
    request: Request,
    bot_token: Optional[str] = Form(default=None),
    bot_name: Optional[str] = Form(default=None),
    is_enabled: bool = Form(default=True),
    avatar: Optional[UploadFile] = File(default=None),
    banner: Optional[UploadFile] = File(default=None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Validate channel exists
    ch_res = await db.execute(select(YouTubeChannel).where(YouTubeChannel.id == channel_id))
    channel = ch_res.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Get or create bot record
    res = await db.execute(select(DiscordBot).where(DiscordBot.channel_id == channel_id))
    db_bot = res.scalar_one_or_none()
    if not db_bot:
        db_bot = DiscordBot(channel_id=channel_id)
        db.add(db_bot)

    if bot_token and bot_token.strip():
        db_bot.bot_token = bot_token.strip()
    if bot_name is not None:
        db_bot.bot_name = bot_name.strip() or None
    db_bot.is_enabled = is_enabled

    # Handle avatar upload
    if avatar and avatar.filename and _allowed_image(avatar.filename):
        path = await _save_upload(avatar, f"ch{channel_id}")
        db_bot.avatar_path = path

    # Handle banner upload
    if banner and banner.filename and _allowed_image(banner.filename):
        path = await _save_upload(banner, f"ch{channel_id}")
        db_bot.banner_path = path

    db_bot.last_error = None
    await db.commit()
    await db.refresh(db_bot)

    # Apply branding in background so save response is instant (under 20ms)
    if db_bot.bot_token:
        import asyncio
        asyncio.create_task(_apply_branding(db_bot))

    return JSONResponse({"ok": True, "message": "Bot configuration saved"})


@router.post("/{channel_id}/start")
async def start_bot(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(DiscordBot).where(DiscordBot.channel_id == channel_id))
    db_bot = res.scalar_one_or_none()
    if not db_bot or not db_bot.bot_token:
        raise HTTPException(status_code=400, detail="No bot token configured for this channel")

    from bot.manager import bot_manager
    await bot_manager.start_bot(
        channel_id=channel_id,
        token=db_bot.bot_token.strip(),
        bot_name=db_bot.bot_name or "YTBot",
    )
    db_bot.is_running = True
    db_bot.last_error = None
    await db.commit()
    return JSONResponse({"ok": True, "message": "Bot started"})


@router.post("/{channel_id}/stop")
async def stop_bot(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from bot.manager import bot_manager
    await bot_manager.stop_bot(channel_id)

    res = await db.execute(select(DiscordBot).where(DiscordBot.channel_id == channel_id))
    db_bot = res.scalar_one_or_none()
    if db_bot:
        db_bot.is_running = False
        await db.commit()
    return JSONResponse({"ok": True, "message": "Bot stopped"})


@router.post("/{channel_id}/restart")
async def restart_bot(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(DiscordBot).where(DiscordBot.channel_id == channel_id))
    db_bot = res.scalar_one_or_none()
    if not db_bot or not db_bot.bot_token:
        raise HTTPException(status_code=400, detail="No bot token configured for this channel")

    from bot.manager import bot_manager
    import asyncio
    asyncio.create_task(
        bot_manager.restart_bot(
            channel_id=channel_id,
            token=db_bot.bot_token.strip(),
            bot_name=db_bot.bot_name or "YTBot",
        )
    )
    db_bot.last_error = None
    await db.commit()
    return JSONResponse({"ok": True, "message": "Bot restart initiated"})


@router.delete("/{channel_id}")
async def delete_bot(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from bot.manager import bot_manager
    await bot_manager.stop_bot(channel_id)

    res = await db.execute(select(DiscordBot).where(DiscordBot.channel_id == channel_id))
    db_bot = res.scalar_one_or_none()
    if db_bot:
        await db.delete(db_bot)
        await db.commit()
    return JSONResponse({"ok": True, "message": "Bot deleted"})


# ── Internal helpers ────────────────────────────────────────────────────────

async def _apply_branding(db_bot: DiscordBot) -> None:
    """Push name/avatar/banner to Discord via REST API."""
    import httpx, base64

    if not db_bot.bot_token:
        return

    headers = {
        "Authorization": f"Bot {db_bot.bot_token}",
        "Content-Type": "application/json",
    }

    payload: dict = {}
    if db_bot.bot_name:
        payload["username"] = db_bot.bot_name

    if db_bot.avatar_path and Path(db_bot.avatar_path).exists():
        data = Path(db_bot.avatar_path).read_bytes()
        ext = Path(db_bot.avatar_path).suffix.lower().lstrip(".")
        mime = "image/gif" if ext == "gif" else f"image/{ext or 'png'}"
        payload["avatar"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"

    if not payload:
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                "https://discord.com/api/v10/users/@me",
                headers=headers,
                json=payload,
            )
            if resp.status_code not in (200, 204):
                from core.logging_config import get_logger
                get_logger(__name__).warning(
                    "Failed to apply branding", status=resp.status_code, body=resp.text[:200]
                )
    except Exception:
        pass
