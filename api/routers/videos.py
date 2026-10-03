"""
api/routers/videos.py
----------------------
Video upload (chunked), metadata configuration, preview.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException,
    Request, UploadFile, status
)
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.config import settings
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token
from models.video import Video
from models.channel import YouTubeChannel
import time
from services.ffmpeg import probe_video, render_real_frame_preview

log = get_logger(__name__)
router = APIRouter(prefix="/videos", tags=["Videos"])

UPLOAD_DIR = Path("uploads/videos")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_UPLOAD_DIR = Path("uploads/temp")
TEMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


async def _ensure_video_schema(db: AsyncSession) -> None:
    """Ensure video schema has thumbnail_id in SQLite."""
    try:
        from sqlalchemy import text
        res = await db.execute(text("PRAGMA table_info(videos)"))
        cols = [r[1] for r in res.fetchall()]
        if cols and "thumbnail_id" not in cols:
            await db.execute(text("ALTER TABLE videos ADD COLUMN thumbnail_id INTEGER REFERENCES thumbnails(id) ON DELETE SET NULL"))
            await db.commit()
    except Exception:
        pass


@router.get("", response_class=HTMLResponse)
async def videos_page(
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_video_schema(db)
    channels_result = await db.execute(select(YouTubeChannel).where(YouTubeChannel.is_active == True))
    channels = channels_result.scalars().all()

    from sqlalchemy.orm import selectinload
    try:
        videos_result = await db.execute(
            select(Video)
            .options(selectinload(Video.channel).selectinload(YouTubeChannel.thumbnails), selectinload(Video.thumbnail))
            .order_by(Video.id.desc())
        )
        videos = videos_result.scalars().all()
    except Exception:
        videos_result = await db.execute(select(Video).order_by(Video.id.desc()))
        videos = videos_result.scalars().all()

    from models.thumbnail import Thumbnail
    thumbs_result = await db.execute(
        select(Thumbnail)
        .where(Thumbnail.is_active == True, ~Thumbnail.filename.like("auto_video_%"))
        .order_by(Thumbnail.sort_order, Thumbnail.id)
    )
    all_thumbnails = thumbs_result.scalars().all()

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "videos.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "videos": videos,
            "thumbnails": all_thumbnails,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp



@router.post("/upload")
async def upload_video(
    request: Request,
    channel_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form("[]"),
    category_id: str = Form("22"),
    privacy_status: str = Form("public"),
    file: UploadFile = File(...),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Receive a chunked video upload, save to disk, probe with ffprobe,
    and create a Video record in the DB.
    """
    # Validate size header
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_video_size_bytes:
        raise HTTPException(status_code=413, detail="Video file too large")

    # Validate channel
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Validate tags JSON
    try:
        tags_list = json.loads(tags)
        if not isinstance(tags_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=422, detail="tags must be a JSON array")

    # Save file
    safe_name = f"{uuid.uuid4()}_{Path(file.filename).name}"
    dest = UPLOAD_DIR / safe_name
    total_size = 0

    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                total_size += len(chunk)
                if total_size > settings.max_video_size_bytes:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Video file too large")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        log.exception("Video save failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to save video")

    # Probe with ffprobe
    duration = None
    try:
        info = await probe_video(str(dest))
        duration = info.get("duration")
    except Exception as exc:
        log.warning("ffprobe failed", error=str(exc))

    # Deactivate previous active video for this channel
    prev_result = await db.execute(
        select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
    )
    for v in prev_result.scalars().all():
        v.is_active = False

    # Create Video record
    video = Video(
        channel_id=channel_id,
        original_filename=file.filename,
        stored_path=str(dest),
        file_size_bytes=total_size,
        duration_seconds=duration,
        title=title[:100],
        description=description,
        tags=json.dumps(tags_list),
        category_id=category_id,
        privacy_status=privacy_status,
        is_active=True,
    )
    db.add(video)
    await db.commit()
    await db.refresh(video)

    manage_url = f"/api/videos/{video.id}/manage"

    log.info("Video uploaded", video_id=video.id, size=total_size, duration=duration)
    return JSONResponse({"ok": True, "video_id": video.id, "duration": duration, "manage_url": manage_url})


@router.post("/upload/chunk")
async def upload_video_chunk(
    request: Request,
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    offset: int = Form(0),
    chunk: UploadFile = File(...),
    user=Depends(require_admin),
):
    """
    Receive a small video slice (e.g. 5-8 MB) for a given upload_id.
    Bypasses reverse proxy and Cloudflare 100MB body limits by slicing large videos.
    """
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", upload_id)
    if not safe_id:
        raise HTTPException(status_code=400, detail="Invalid upload_id")

    part_path = TEMP_UPLOAD_DIR / f"{safe_id}.part"

    try:
        content = await chunk.read()
        mode = "r+b" if part_path.exists() else "wb"
        with open(part_path, mode) as f:
            f.seek(offset)
            f.write(content)
            f.flush()
    except Exception as exc:
        log.exception("Chunk write failed", upload_id=safe_id, chunk_index=chunk_index, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to write chunk {chunk_index}")

    return {
        "ok": True,
        "upload_id": safe_id,
        "chunk_index": chunk_index,
        "bytes_written": len(content),
    }


@router.post("/upload/complete")
async def complete_video_upload(
    request: Request,
    upload_id: str = Form(...),
    channel_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form("[]"),
    category_id: str = Form("22"),
    privacy_status: str = Form("public"),
    filename: str = Form(...),
    total_size: int = Form(...),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Finalize the chunked upload: verify total size, move file to destination,
    probe with ffprobe, and create database record.
    """
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", upload_id)
    part_path = TEMP_UPLOAD_DIR / f"{safe_id}.part"

    if not part_path.exists():
        raise HTTPException(status_code=404, detail="Upload file parts not found or expired")

    actual_size = part_path.stat().st_size
    if actual_size > settings.max_video_size_bytes:
        part_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="Video file exceeds maximum allowed size")

    # Validate channel
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        part_path.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="Channel not found")

    # Validate tags
    try:
        tags_list = json.loads(tags)
        if not isinstance(tags_list, list):
            tags_list = [t.strip() for t in str(tags).split(",") if t.strip()]
    except Exception:
        tags_list = [t.strip() for t in str(tags).split(",") if t.strip()]

    # Move to final storage
    safe_name = f"{uuid.uuid4()}_{Path(filename).name}"
    dest = UPLOAD_DIR / safe_name
    shutil.move(str(part_path), str(dest))

    # Probe with ffprobe
    duration = None
    try:
        info = await probe_video(str(dest))
        duration = info.get("duration")
    except Exception as exc:
        log.warning("ffprobe failed", error=str(exc))

    # Deactivate previous active video for this channel
    prev_result = await db.execute(
        select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
    )
    for v in prev_result.scalars().all():
        v.is_active = False

    video = Video(
        channel_id=channel_id,
        original_filename=Path(filename).name,
        stored_path=str(dest),
        file_size_bytes=actual_size,
        duration_seconds=duration,
        title=title[:100],
        description=description,
        tags=json.dumps(tags_list),
        category_id=category_id,
        privacy_status=privacy_status,
        is_active=True,
    )
    db.add(video)
    await db.commit()
    await db.refresh(video)

    manage_url = f"/api/videos/{video.id}/manage"

    log.info("Chunked video upload finalized", video_id=video.id, size=actual_size, duration=duration)
    return JSONResponse({
        "ok": True,
        "video_id": video.id,
        "duration": duration,
        "manage_url": manage_url,
    })


@router.get("/{video_id}/manage", response_class=HTMLResponse)
async def video_manager_page(
    video_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Studio / Video Manager page to configure on-screen timestamp overlays and metadata."""
    await _ensure_video_schema(db)
    from sqlalchemy.orm import selectinload
    try:
        res = await db.execute(
            select(Video)
            .options(selectinload(Video.channel).selectinload(YouTubeChannel.thumbnails), selectinload(Video.thumbnail))
            .where(Video.id == video_id)
        )
        video = res.scalar_one_or_none()
    except Exception:
        video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    channel = await db.get(YouTubeChannel, video.channel_id)
    csrf = generate_csrf_token()

    from scheduler.jobs import generate_random_code
    from models.thumbnail import Thumbnail
    active_username = generate_random_code()
    active_password = generate_random_code(exclude=active_username)

    thumbs_res = await db.execute(
        select(Thumbnail)
        .where(
            Thumbnail.channel_id == channel.id,
            Thumbnail.is_active == True,
            ~Thumbnail.filename.like("auto_video_%"),
        )
        .order_by(Thumbnail.sort_order, Thumbnail.id)
    )
    thumbnails = thumbs_res.scalars().all()

    resp = templates.TemplateResponse(
        "video_manager.html",
        {
            "request": request,
            "user": user,
            "video": video,
            "channel": channel,
            "thumbnails": thumbnails,
            "admin_username": settings.admin_username,
            "active_username": active_username,
            "active_password": active_password,
            "csrf_token": csrf,
        },
    )

    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("/{video_id}/overlays")
async def update_video_overlays(
    video_id: int,
    overlay_username_enabled: bool = Form(False),
    overlay_username_text: str = Form("User: {username}"),
    overlay_username_start: float = Form(5.0),
    overlay_username_end: float = Form(15.0),
    overlay_username_position: str = Form("top-left"),
    overlay_username_font_size: int = Form(36),
    overlay_username_color: str = Form("#ffffff"),
    overlay_randomize_username: bool = Form(True),
    overlay_password_enabled: bool = Form(False),
    overlay_password_text: str = Form("Pass: {password}"),
    overlay_password_start: float = Form(20.0),
    overlay_password_end: float = Form(30.0),
    overlay_password_position: str = Form("bottom-right"),
    overlay_password_font_size: int = Form(36),
    overlay_password_color: str = Form("#22c55e"),
    overlay_randomize_password: bool = Form(True),
    overlay_randomize_time: bool = Form(False),
    overlay_random_u_min: float = Form(5.0),
    overlay_random_u_max: float = Form(30.0),
    overlay_random_p_min: float = Form(25.0),
    overlay_random_p_max: float = Form(60.0),
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    category_id: Optional[str] = Form(None),
    privacy_status: Optional[str] = Form(None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Save overlay timestamps and metadata for a video."""
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    video.overlay_username_enabled = overlay_username_enabled
    video.overlay_username_text = overlay_username_text
    video.overlay_username_start = overlay_username_start
    video.overlay_username_end = overlay_username_end
    video.overlay_username_position = overlay_username_position
    video.overlay_username_font_size = overlay_username_font_size
    video.overlay_username_color = overlay_username_color
    video.overlay_randomize_username = overlay_randomize_username

    video.overlay_password_enabled = overlay_password_enabled
    video.overlay_password_text = overlay_password_text
    video.overlay_password_start = overlay_password_start
    video.overlay_password_end = overlay_password_end
    video.overlay_password_position = overlay_password_position
    video.overlay_password_font_size = overlay_password_font_size
    video.overlay_password_color = overlay_password_color
    video.overlay_randomize_password = overlay_randomize_password

    video.overlay_randomize_time = overlay_randomize_time
    video.overlay_random_u_min = overlay_random_u_min
    video.overlay_random_u_max = overlay_random_u_max
    video.overlay_random_p_min = overlay_random_p_min
    video.overlay_random_p_max = overlay_random_p_max

    if overlay_username_enabled or overlay_password_enabled:
        video.apply_ffmpeg = True

    if title:
        video.title = title[:100]
    if description is not None:
        video.description = description
    if category_id:
        video.category_id = category_id
    if privacy_status:
        video.privacy_status = privacy_status

    await db.commit()
    log.info("Video overlay configuration saved", video_id=video.id)
    return {"ok": True, "video_id": video.id}


@router.post("/{video_id}/real-position-preview")
async def get_real_position_preview(
    video_id: int,
    timestamp: Optional[float] = Form(None),
    u_enabled: bool = Form(True),
    u_text: str = Form("User: {username}"),
    u_pos: str = Form("safe-top-left"),
    u_size: int = Form(36),
    u_color: str = Form("#ffffff"),
    p_enabled: bool = Form(True),
    p_text: str = Form("Pass: {password}"),
    p_pos: str = Form("safe-bottom-right"),
    p_size: int = Form(36),
    p_color: str = Form("#22c55e"),
    sample_user: str = Form("abc"),
    sample_pass: str = Form("free"),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate an instant real frame snapshot with FFmpeg showing the exact
    real position of the User and Pass overlays burned into the video.
    """
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not os.path.exists(video.stored_path):
        raise HTTPException(status_code=404, detail="Video source file missing from disk")

    preview_dir = Path("uploads/previews")
    preview_dir.mkdir(parents=True, exist_ok=True)

    # Clean old preview files (keep directory tidy)
    try:
        existing = sorted(preview_dir.glob("*.jpg"), key=os.path.getmtime)
        if len(existing) > 30:
            for old in existing[:-20]:
                old.unlink(missing_ok=True)
    except Exception:
        pass

    # Resolve timestamp
    t = timestamp
    if t is None:
        if video.overlay_password_start is not None and p_enabled:
            t = float(video.overlay_password_start)
        elif video.overlay_username_start is not None and u_enabled:
            t = float(video.overlay_username_start)
        else:
            t = 5.0

    overlays = []
    if u_enabled and u_text:
        overlays.append({
            "enabled": True,
            "text": u_text.replace("{username}", sample_user),
            "position": u_pos,
            "fontsize": u_size,
            "color": u_color,
            "always_visible": True,
        })
    if p_enabled and p_text:
        overlays.append({
            "enabled": True,
            "text": p_text.replace("{password}", sample_pass),
            "position": p_pos,
            "fontsize": p_size,
            "color": p_color,
            "always_visible": True,
        })

    out_filename = f"preview_real_{video.id}_{uuid.uuid4().hex[:8]}.jpg"
    out_path = str(preview_dir / out_filename)

    try:
        res = await render_real_frame_preview(
            video_path=video.stored_path,
            timestamp=t,
            overlays=overlays,
            output_path=out_path,
        )
    except Exception as exc:
        log.exception("Real preview frame generation failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"FFmpeg error: {exc}")

    return {
        "ok": True,
        "preview_url": f"/uploads/previews/{out_filename}?t={int(time.time()*1000)}",
        "timestamp": res["timestamp"],
        "width": res["width"],
        "height": res["height"],
        "duration": res["duration"],
        "u_pos": u_pos,
        "p_pos": p_pos,
        "sample_user": sample_user,
        "sample_pass": sample_pass,
    }


@router.get("/{video_id}")
async def get_video_details(
    video_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch video metadata along with channel Discord server invite link."""
    from models.discord_config import DiscordConfig
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    disc_res = await db.execute(
        select(DiscordConfig).where(DiscordConfig.channel_id == video.channel_id)
    )
    disc_cfg = disc_res.scalar_one_or_none()
    discord_server_url = disc_cfg.button_url_2 if disc_cfg and disc_cfg.button_url_2 else ""

    try:
        tags_list = json.loads(video.tags) if video.tags else []
    except Exception:
        tags_list = []

    return {
        "ok": True,
        "id": video.id,
        "channel_id": video.channel_id,
        "title": video.title,
        "description": video.description or "",
        "tags": tags_list,
        "category_id": video.category_id or "22",
        "privacy_status": video.privacy_status or "public",
        "discord_server_url": discord_server_url,
        "thumbnail_id": video.thumbnail_id,
        "thumbnail_url": video.thumbnail_web_url,
        "thumbnail_name": video.thumbnail_name,
    }


@router.patch("/{video_id}")
async def update_video(
    video_id: int,
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        title = data.get("title")
        description = data.get("description")
        tags = data.get("tags")
        category_id = data.get("category_id")
        privacy_status = data.get("privacy_status")
        apply_ffmpeg = data.get("apply_ffmpeg")
        trim_start = data.get("trim_start")
        trim_end = data.get("trim_end")
        thumbnail_id = data.get("thumbnail_id") if "thumbnail_id" in data else None
        has_thumbnail_id = "thumbnail_id" in data
    else:
        form = await request.form()
        title = form.get("title")
        description = form.get("description")
        tags = form.get("tags")
        category_id = form.get("category_id")
        privacy_status = form.get("privacy_status")
        apply_ffmpeg = form.get("apply_ffmpeg")
        trim_start = form.get("trim_start")
        trim_end = form.get("trim_end")
        thumbnail_id = form.get("thumbnail_id")
        has_thumbnail_id = "thumbnail_id" in form

    if title is not None:
        video.title = str(title).strip()[:100]
    if description is not None:
        video.description = str(description).strip()
    if tags is not None:
        if isinstance(tags, list):
            video.tags = json.dumps(tags)
        else:
            try:
                video.tags = json.dumps(json.loads(tags))
            except json.JSONDecodeError:
                # If comma-separated string
                items = [t.strip() for t in str(tags).split(",") if t.strip()]
                video.tags = json.dumps(items)
    if category_id is not None:
        video.category_id = str(category_id)
    if privacy_status is not None:
        video.privacy_status = str(privacy_status)
    if apply_ffmpeg is not None:
        video.apply_ffmpeg = bool(apply_ffmpeg)
    if trim_start is not None and trim_start != "":
        video.trim_start = float(trim_start)
    if trim_end is not None and trim_end != "":
        video.trim_end = float(trim_end)
    if has_thumbnail_id:
        if thumbnail_id and str(thumbnail_id).isdigit() and int(thumbnail_id) > 0:
            video.thumbnail_id = int(thumbnail_id)
        else:
            video.thumbnail_id = None

    await db.flush()
    return {
        "ok": True,
        "video_id": video.id,
        "title": video.title,
        "thumbnail_id": video.thumbnail_id,
        "thumbnail_url": video.thumbnail_web_url,
    }



@router.delete("/{video_id}")
async def delete_video(
    video_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        Path(video.stored_path).unlink(missing_ok=True)
    except Exception:
        pass
    await db.delete(video)
    await db.commit()
    return {"ok": True}


@router.post("/{video_id}/set-active")
async def set_video_active_endpoint(
    video_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set this video as the active video to be uploaded in the schedule."""
    target_vid = await db.get(Video, video_id)
    if not target_vid:
        raise HTTPException(status_code=404, detail="Video not found")

    # Set all other videos for this channel to is_active = False
    all_vids = (await db.execute(
        select(Video).where(Video.channel_id == target_vid.channel_id)
    )).scalars().all()
    for v in all_vids:
        v.is_active = (v.id == video_id)

    await db.commit()
    log.info("Video set active for schedule", video_id=video_id, channel_id=target_vid.channel_id, title=target_vid.title)
    return {
        "ok": True,
        "video_id": video_id,
        "channel_id": target_vid.channel_id,
        "title": target_vid.title,
        "message": f"'{target_vid.title}' is now scheduled for upload",
    }


@router.post("/{video_id}/test-upload")
async def test_upload_endpoint(
    video_id: int,
    privacy: str = Form("unlisted"),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an immediate test upload of this video to YouTube."""
    from scheduler.jobs import test_upload_video
    from services.youtube import UploadLimitExceededError, QuotaExceededError
    try:
        result = await test_upload_video(video_id=video_id, privacy=privacy)
        return result
    except UploadLimitExceededError as exc:
        log.warning("Test upload failed due to daily upload limit", video_id=video_id)
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "upload_limit_exceeded",
                "message": "YouTube Daily Upload Limit Exceeded: This channel has reached Google's maximum video uploads for today (24-hour limit).",
                "resolution": "To resolve this in hosting: (1) Unlock 'Advanced features' in YouTube Studio (Settings > Channel > Feature eligibility) via 30s Video Verification or ID to unlock 100+ uploads/day, (2) Switch to another YouTube channel in the Channels tab, or (3) Wait 24 hours for Google's rolling limit to reset.",
                "studio_url": "https://studio.youtube.com/",
            },
        )
    except QuotaExceededError as exc:
        log.warning("Test upload failed due to API quota exceeded", video_id=video_id)
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "quota_exceeded",
                "message": "YouTube API Quota Exceeded (10,000 units/day project cap).",
                "resolution": "Your Google Cloud Project has consumed all quota units for today. The quota resets at midnight Pacific Time (PT).",
            },
        )
    except Exception as exc:
        err_str = str(exc)
        if "uploadLimitExceeded" in err_str or "exceeded the number of videos" in err_str:
            raise HTTPException(
                status_code=429,
                detail={
                    "error_type": "upload_limit_exceeded",
                    "message": "YouTube Daily Upload Limit Exceeded: This channel has reached Google's maximum video uploads for today (24-hour limit).",
                    "resolution": "To resolve this in hosting: (1) Unlock 'Advanced features' in YouTube Studio (Settings > Channel > Feature eligibility) via 30s Video Verification or ID to unlock 100+ uploads/day, (2) Switch to another YouTube channel in the Channels tab, or (3) Wait 24 hours for Google's rolling limit to reset.",
                    "studio_url": "https://studio.youtube.com/",
                },
            )
        log.exception("Test upload failed", video_id=video_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{video_id}/upload-progress")
async def get_video_upload_progress(
    video_id: int,
    user=Depends(require_admin),
):
    """Return live real-time progress of video encoding and YouTube upload."""
    from services.upload_progress import upload_tracker
    prog = upload_tracker.get(video_id)
    if not prog:
        return {"active": False, "stage": "idle", "percent": 0}
    return prog


@router.post("/{video_id}/set-thumbnail")
async def set_video_thumbnail(
    video_id: int,
    thumbnail_id: Optional[int] = Form(None),
    file: Optional[UploadFile] = File(None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Assign ANY thumbnail from stock, or upload a brand new thumbnail for this video.
    If thumbnail_id == 0 or not provided and no file: resets to Channel Stock (Auto-Rotate).
    """
    await _ensure_video_schema(db)
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    from models.thumbnail import Thumbnail
    from services.thumbnail import validate_thumbnail, standardize_thumbnail
    import uuid

    if file and file.filename:
        content = await file.read(11 * 1024 * 1024)
        safe_name = f"{uuid.uuid4()}_{Path(file.filename).name}"
        thumb_dir = Path("uploads/thumbnails")
        thumb_dir.mkdir(parents=True, exist_ok=True)
        dest = thumb_dir / safe_name
        dest.write_bytes(content)

        w, h, sz = standardize_thumbnail(str(dest), target_width=1280, target_height=720)

        thumb = Thumbnail(
            channel_id=video.channel_id,
            filename=file.filename,
            stored_path=str(dest),
            width=w,
            height=h,
            file_size_bytes=sz,
            sort_order=0,
            is_active=True,
        )
        db.add(thumb)
        await db.flush()
        video.thumbnail_id = thumb.id
    elif thumbnail_id is not None:
        if thumbnail_id > 0:
            thumb = await db.get(Thumbnail, thumbnail_id)
            if not thumb:
                raise HTTPException(status_code=404, detail="Thumbnail not found")
            video.thumbnail_id = thumb.id
        else:
            video.thumbnail_id = None

    await db.commit()
    await db.refresh(video)

    return JSONResponse({
        "ok": True,
        "video_id": video.id,
        "thumbnail_id": video.thumbnail_id,
        "thumbnail_url": video.thumbnail_web_url,
        "thumbnail_name": video.thumbnail_name,
        "message": "Thumbnail updated successfully!",
    })


@router.post("/{video_id}/auto-thumbnail")
async def generate_video_auto_thumbnail(
    video_id: int,
    target_sec: float = Form(1.0),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fallback endpoint for backward compatibility."""
    video = await db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    from services.thumbnail import get_channel_thumbnails
    thumbs = await get_channel_thumbnails(db, video.channel_id)
    if thumbs:
        video.thumbnail_id = thumbs[0].id
        await db.commit()
        return JSONResponse({
            "ok": True,
            "video_id": video.id,
            "thumbnail_id": thumbs[0].id,
            "thumbnail_url": thumbs[0].web_url,
            "width": thumbs[0].width,
            "height": thumbs[0].height,
            "timestamp": target_sec,
        })

    return JSONResponse({
        "ok": True,
        "video_id": video.id,
        "thumbnail_id": None,
        "thumbnail_url": video.thumbnail_web_url,
    })



