"""
api/routers/thumbnails.py
--------------------------
Thumbnail pool management: upload, list, delete, reorder.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import List

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException,
    Request, UploadFile
)
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token
from models.thumbnail import Thumbnail
from models.channel import YouTubeChannel
from services.thumbnail import (
    ThumbnailValidationError,
    validate_thumbnail,
    standardize_thumbnail,
    reorder_thumbnails,
)

log = get_logger(__name__)
router = APIRouter(prefix="/thumbnails", tags=["Thumbnails"])

THUMB_DIR = Path("uploads/thumbnails")
THUMB_DIR.mkdir(parents=True, exist_ok=True)


@router.get("/{thumbnail_id}/image")
@router.get("/{thumbnail_id}/file")
async def get_thumbnail_image(thumbnail_id: int, db: AsyncSession = Depends(get_db)):
    """Serve the thumbnail image directly with caching and automatic fallback."""
    thumb = await db.get(Thumbnail, thumbnail_id)
    if not thumb:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    import os
    from pathlib import Path
    from fastapi.responses import FileResponse, Response

    p = Path(thumb.stored_path)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p

    if not p.exists():
        # Check by filename in THUMB_DIR
        alt = THUMB_DIR / Path(thumb.stored_path).name
        if alt.exists():
            p = alt
        else:
            alt2 = THUMB_DIR / thumb.filename
            if alt2.exists():
                p = alt2

    if p.exists() and p.stat().st_size > 0:
        return FileResponse(
            str(p),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # If file was deleted or lost, generate a clean 1280x720 fallback so UI never shows broken image
    from PIL import Image, ImageDraw
    import io
    fallback_img = Image.new("RGB", (1280, 720), (15, 23, 42))
    d = ImageDraw.Draw(fallback_img)
    d.rectangle([(20, 20), (1260, 700)], outline=(56, 189, 248), width=3)
    d.text((640, 360), thumb.filename or "Thumbnail", fill=(255, 255, 255), anchor="mm")
    buf = io.BytesIO()
    fallback_img.save(buf, "JPEG", quality=85)
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@router.get("", response_class=HTMLResponse)
async def thumbnails_page(
    request: Request,
    channel_id: int = 0,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channels_result = await db.execute(select(YouTubeChannel).where(YouTubeChannel.is_active == True))
    channels = channels_result.scalars().all()

    if not channel_id and channels:
        channel_id = channels[0].id

    # Purge any legacy auto_video thumbnails so stock is clean
    try:
        auto_del = await db.execute(
            select(Thumbnail).where(Thumbnail.filename.like("auto_video_%"))
        )
        auto_records = auto_del.scalars().all()
        if auto_records:
            for ad in auto_records:
                try:
                    Path(ad.stored_path).unlink(missing_ok=True)
                except Exception:
                    pass
                await db.delete(ad)
            await db.commit()
    except Exception:
        pass

    thumbs = []
    if channel_id:
        result = await db.execute(
            select(Thumbnail)
            .where(
                Thumbnail.channel_id == channel_id,
                Thumbnail.is_active == True,
                ~Thumbnail.filename.like("auto_video_%"),
            )
            .order_by(Thumbnail.sort_order, Thumbnail.id)
        )
        thumbs = result.scalars().all()


    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "thumbnails.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "thumbnails": thumbs,
            "selected_channel_id": channel_id,
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("/upload")
async def upload_thumbnail(
    channel_id: int = Form(...),
    file: UploadFile = File(...),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Upload, standardize to YouTube working size (1280x720), and validate a thumbnail image."""
    # Read into memory (max 10 MB)
    content = await file.read(11 * 1024 * 1024)
    file_size = len(content)

    # Save temporarily for Pillow validation
    safe_name = f"{uuid.uuid4()}_{Path(file.filename).name}"
    dest = THUMB_DIR / safe_name
    dest.write_bytes(content)

    try:
        width, height = validate_thumbnail(str(dest), file_size)
    except ThumbnailValidationError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc))

    # Standardize image to exact YouTube working size: 1280x720, < 2MB JPEG
    try:
        width, height, file_size = standardize_thumbnail(str(dest), target_width=1280, target_height=720)
    except Exception as exc:
        log.warning("Could not standardize thumbnail", error=str(exc))

    # Determine next sort order
    result = await db.execute(
        select(Thumbnail).where(Thumbnail.channel_id == channel_id).order_by(Thumbnail.sort_order.desc())
    )
    existing = result.scalars().first()
    next_order = (existing.sort_order + 1) if existing else 0

    thumb = Thumbnail(
        channel_id=channel_id,
        filename=file.filename,
        stored_path=str(dest),
        width=width,
        height=height,
        file_size_bytes=file_size,
        sort_order=next_order,
    )
    db.add(thumb)
    await db.flush()

    log.info("Thumbnail uploaded & standardized to 1280x720", thumb_id=thumb.id, channel_id=channel_id)
    return JSONResponse({
        "ok": True,
        "thumbnail_id": thumb.id,
        "web_url": thumb.web_url,
        "width": width,
        "height": height,
    })


@router.delete("/{thumbnail_id}")
async def delete_thumbnail(
    thumbnail_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    thumb = await db.get(Thumbnail, thumbnail_id)
    if not thumb:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    Path(thumb.stored_path).unlink(missing_ok=True)
    await db.delete(thumb)
    return {"ok": True}


@router.post("/reorder")
async def reorder_thumbnail_list(
    channel_id: int = Form(...),
    ordered_ids: str = Form(...),  # JSON array of int IDs
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    import json
    try:
        ids: List[int] = json.loads(ordered_ids)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=422, detail="ordered_ids must be a JSON array of integers")

    await reorder_thumbnails(db, channel_id, ids)
    return {"ok": True}


@router.get("/api/{channel_id}")
async def list_thumbnails_api(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Thumbnail)
        .where(Thumbnail.channel_id == channel_id, Thumbnail.is_active == True)
        .order_by(Thumbnail.sort_order, Thumbnail.id)
    )
    thumbs = result.scalars().all()
    return [
        {
            "id": t.id,
            "filename": t.filename,
            "width": t.width,
            "height": t.height,
            "sort_order": t.sort_order,
            "last_used_at": t.last_used_at,
        }
        for t in thumbs
    ]


@router.post("/auto-generate")
async def auto_generate_thumbnail_endpoint(
    channel_id: int = Form(...),
    video_id: Optional[int] = Form(None),
    target_sec: float = Form(1.0),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-extract a high-definition 1.0-second photo thumbnail from the video
    and add it directly to the channel's thumbnail stock pool.
    """
    from models.video import Video
    from services.thumbnail import ensure_video_thumbnail

    if video_id:
        video = await db.get(Video, video_id)
    else:
        res = await db.execute(
            select(Video)
            .where(Video.channel_id == channel_id, Video.is_active == True)
            .order_by(Video.id.desc())
            .limit(1)
        )
        video = res.scalar_one_or_none()
        if not video:
            res_any = await db.execute(select(Video).where(Video.is_active == True).order_by(Video.id.desc()).limit(1))
            video = res_any.scalar_one_or_none()

    if not video:
        raise HTTPException(status_code=404, detail="No video found to generate thumbnail from")

    thumb = await ensure_video_thumbnail(
        db=db,
        channel_id=channel_id,
        video=video,
        target_sec=target_sec,
        force_refresh=True,
    )
    if not thumb:
        raise HTTPException(status_code=500, detail="Failed to extract thumbnail frame from video")

    await db.commit()
    return JSONResponse({
        "ok": True,
        "thumbnail_id": thumb.id,
        "web_url": thumb.web_url,
        "filename": thumb.filename,
        "width": thumb.width,
        "height": thumb.height,
    })

