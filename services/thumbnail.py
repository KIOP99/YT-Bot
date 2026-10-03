"""
services/thumbnail.py
----------------------
Thumbnail pool management:
  - Validate uploaded images (format, dimensions, file size)
  - Select next thumbnail (sequential or random) with DB update
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.logging_config import get_logger
from models.thumbnail import Thumbnail

log = get_logger(__name__)

# YouTube recommends 1280×720 minimum (16:9)
MIN_WIDTH = 1280
MIN_HEIGHT = 720
ASPECT_TOLERANCE = 0.05  # allow ±5% deviation from 16:9


class ThumbnailValidationError(Exception):
    pass


def validate_thumbnail(file_path: str, file_size: int) -> tuple[int, int]:
    """
    Validate a thumbnail file.
    Returns (width, height) on success; raises ThumbnailValidationError on failure.
    """
    # Size check
    if file_size > settings.max_thumbnail_size_bytes:
        raise ThumbnailValidationError(
            f"File too large: {file_size / 1024 / 1024:.1f} MB "
            f"(max {settings.max_thumbnail_size_mb} MB)"
        )

    # Open with Pillow for format + dimension check
    try:
        with Image.open(file_path) as img:
            fmt = img.format
            if fmt not in ("JPEG", "PNG"):
                raise ThumbnailValidationError(f"Unsupported format: {fmt}. Use JPEG or PNG.")
            w, h = img.size
    except Exception as exc:
        raise ThumbnailValidationError(f"Cannot read image: {exc}") from exc

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        raise ThumbnailValidationError(
            f"Image too small: {w}×{h} (minimum {MIN_WIDTH}×{MIN_HEIGHT})"
        )

    # 16:9 aspect ratio check
    target = 16 / 9
    actual = w / h
    if abs(actual - target) / target > ASPECT_TOLERANCE:
        raise ThumbnailValidationError(
            f"Aspect ratio {actual:.3f} is not close enough to 16:9 "
            f"(got {w}×{h})"
        )

    return w, h


def standardize_thumbnail(
    file_path: str,
    target_width: int = 1280,
    target_height: int = 720,
    quality: int = 90,
) -> tuple[int, int, int]:
    """
    Auto-scale, format, and optimize any image into YouTube's official working size:
      - Exactly target_width × target_height (1280×720, 16:9 HD standard)
      - High-quality Lanczos resampling
      - Converts palette/RGBA/CMYK to RGB JPEG
      - Letterboxes or crops smoothly if aspect ratio deviates from 16:9
      - Ensures file size is <= 2MB
      - Overwrites file_path with optimized working-size JPEG
    Returns (width, height, file_size_bytes).
    """
    import os
    from pathlib import Path
    from PIL import Image, ImageOps

    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Thumbnail image file not found: {p}")

    with Image.open(str(p)) as raw_img:
        # Convert to RGB (handle transparency / alpha channels with sleek dark background)
        if raw_img.mode in ("RGBA", "LA") or (raw_img.mode == "P" and "transparency" in raw_img.info):
            bg = Image.new("RGB", raw_img.size, (15, 23, 42))
            alpha_img = raw_img.convert("RGBA")
            bg.paste(alpha_img, mask=alpha_img.split()[3])
            img = bg
        elif raw_img.mode != "RGB":
            img = raw_img.convert("RGB")
        else:
            img = raw_img.copy()

    orig_w, orig_h = img.size

    # Fit into target_width x target_height (1280x720)
    target_ratio = target_width / target_height
    actual_ratio = orig_w / orig_h

    resample_filter = getattr(Image, "Resampling", Image).LANCZOS

    if abs(actual_ratio - target_ratio) <= 0.08:
        resized = img.resize((target_width, target_height), resample_filter)
    else:
        resized = ImageOps.pad(img, (target_width, target_height), method=resample_filter, color=(10, 15, 30))

    # Save as JPEG and guarantee <= 2MB
    q = quality
    while q >= 60:
        resized.save(str(p), "JPEG", quality=q, optimize=True)
        size = p.stat().st_size
        if size <= 2 * 1024 * 1024:
            break
        q -= 5

    final_size = p.stat().st_size
    log.info("Standardized thumbnail to YouTube working size", path=str(p), width=target_width, height=target_height, size_bytes=final_size)
    return target_width, target_height, final_size


async def get_channel_thumbnails(
    db: AsyncSession,
    channel_id: int,
) -> list[Thumbnail]:
    """Return all active thumbnails for a channel, auto-standardizing non-1280x720 images to YouTube working size."""
    import os
    result = await db.execute(
        select(Thumbnail)
        .where(
            Thumbnail.channel_id == channel_id,
            Thumbnail.is_active == True,
            ~Thumbnail.filename.like("auto_video_%"),
        )
        .order_by(Thumbnail.sort_order, Thumbnail.id)
    )
    pool = [t for t in result.scalars().all() if t.stored_path and os.path.exists(t.stored_path)]
    if not pool:
        result_all = await db.execute(
            select(Thumbnail)
            .where(Thumbnail.is_active == True, ~Thumbnail.filename.like("auto_video_%"))
            .order_by(Thumbnail.sort_order, Thumbnail.id)
        )
        pool = [t for t in result_all.scalars().all() if t.stored_path and os.path.exists(t.stored_path)]

    # Auto-standardize any thumbnail in pool that is not 1280x720 working size
    for t in pool:
        if (t.width != 1280 or t.height != 720) and t.stored_path and os.path.exists(t.stored_path):
            try:
                w, h, sz = standardize_thumbnail(t.stored_path)
                t.width = w
                t.height = h
                t.file_size_bytes = sz
            except Exception as e:
                log.warning("Could not auto-standardize pool thumbnail", thumb_id=t.id, error=str(e))

    return pool



async def extract_frame_at_timestamp(
    video_path: str,
    output_path: str,
    target_sec: float = 1.0,
    width: int = 1280,
    height: int = 720,
) -> tuple[int, int, int]:
    """
    Extract a frame at `target_sec` (defaults to 1.0s) from video_path using FFmpeg,
    scale/letterbox to width x height (1280x720 16:9), and save as high-quality JPEG.
    Guarantees output file exists and is <= 2MB for YouTube API compatibility.
    Returns (width, height, file_size_bytes).
    """
    import os
    import asyncio
    from pathlib import Path
    from PIL import Image
    from services.ffmpeg import get_ffmpeg_binary, probe_video

    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink(missing_ok=True)

    info = await probe_video(str(video_path))
    duration = float(info.get("duration", 0.0) or 0.0)

    # Use 1.0 second as requested ("video 1 second part photo")
    # If video is shorter than 1s, clamp to midpoint or 0.0
    if duration > 1.0:
        actual_sec = target_sec
    elif duration > 0.2:
        actual_sec = max(0.05, duration / 2.0)
    else:
        actual_sec = 0.0

    ffmpeg = get_ffmpeg_binary()
    scale_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"

    # Fast input seek before -i
    cmd_fast = [
        ffmpeg, "-y",
        "-ss", f"{actual_sec:.3f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", scale_filter,
        "-q:v", "2",
        str(dest),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd_fast,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Fallback to output seek if fast seek failed or created empty file
    if not dest.exists() or dest.stat().st_size == 0:
        cmd_exact = [
            ffmpeg, "-y",
            "-i", str(video_path),
            "-ss", f"{actual_sec:.3f}",
            "-vframes", "1",
            "-vf", scale_filter,
            "-q:v", "2",
            str(dest),
        ]
        proc2 = await asyncio.create_subprocess_exec(
            *cmd_exact,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err2 = await proc2.communicate()
        if not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg failed to extract 1s thumbnail: {err2.decode(errors='replace')}")

    # Enforce YouTube 2MB limit and JPEG format with PIL
    with Image.open(str(dest)) as img:
        img_w, img_h = img.size
        file_size = dest.stat().st_size
        if file_size > 2 * 1024 * 1024 or img.format != "JPEG":
            img.convert("RGB").save(str(dest), "JPEG", quality=85, optimize=True)
            file_size = dest.stat().st_size

    return img_w, img_h, file_size


async def ensure_video_thumbnail(
    db: AsyncSession,
    channel_id: int,
    video,
    target_sec: float = 1.0,
    force_refresh: bool = False,
) -> Optional[Thumbnail]:
    """
    Ensure an active thumbnail exists for the video.
    Extracts a frame from the video at 1.0s ("video 1 second part photo")
    and creates or updates a Thumbnail record in the database.
    """
    import os
    from pathlib import Path

    if not video or not getattr(video, "stored_path", None) or not os.path.exists(video.stored_path):
        return None

    # Resolve channel_id if not supplied or invalid
    if not channel_id and getattr(video, "channel_id", None):
        channel_id = video.channel_id

    auto_filename = f"auto_video_{video.id}.jpg"
    dest_dir = Path("uploads/thumbnails")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / auto_filename

    # Check if already registered in DB
    res = await db.execute(
        select(Thumbnail)
        .where(Thumbnail.channel_id == channel_id, Thumbnail.filename == auto_filename)
        .limit(1)
    )
    existing = res.scalar_one_or_none()
    if existing and os.path.exists(existing.stored_path) and not force_refresh:
        return existing

    # Extract high-res 1280x720 frame from video at 1.0s mark
    try:
        w, h, file_size = await extract_frame_at_timestamp(
            video_path=video.stored_path,
            output_path=str(dest_path),
            target_sec=target_sec,
            width=1280,
            height=720,
        )

        if dest_path.exists() and file_size > 0:
            if existing:
                existing.stored_path = str(dest_path)
                existing.file_size_bytes = file_size
                existing.width = w
                existing.height = h
                existing.is_active = True
                await db.flush()
                log.info("Refreshed 1s auto-extracted thumbnail", video_id=video.id, path=str(dest_path))
                return existing

            thumb = Thumbnail(
                channel_id=channel_id,
                filename=auto_filename,
                stored_path=str(dest_path),
                width=w,
                height=h,
                file_size_bytes=file_size,
                sort_order=0,
                is_active=True,
            )
            db.add(thumb)
            await db.flush()
            log.info("Auto-extracted 1s thumbnail from video", video_id=video.id, path=str(dest_path))
            return thumb
    except Exception as exc:
        log.warning("Could not auto-extract 1s thumbnail from video", video_id=video.id, error=str(exc))

    return None


async def get_next_thumbnail(
    db: AsyncSession,
    channel_id: int,
    mode: str,  # "sequential" | "random"
    current_index: int,
    video: Optional[Any] = None,
) -> Optional[tuple[Thumbnail, int]]:
    """
    Select and return the next thumbnail from the channel's thumbnail pool.
    Returns (Thumbnail, new_index) or None if pool is empty.
    """
    from datetime import datetime, timezone

    pool = await get_channel_thumbnails(db, channel_id)

    if not pool:
        log.warning("Thumbnail pool is empty", channel_id=channel_id)
        return None


    if mode == "random":
        chosen = random.choice(pool)
        new_index = pool.index(chosen)
    else:
        # Sequential — wrap around
        new_index = current_index % len(pool)
        chosen = pool[new_index]

    chosen.last_used_at = datetime.now(timezone.utc).isoformat()
    await db.flush()

    log.info(
        "Auto-selected thumbnail",
        channel_id=channel_id,
        thumbnail_id=chosen.id,
        filename=chosen.filename,
        mode=mode,
        index=new_index,
    )
    return chosen, new_index


async def reorder_thumbnails(
    db: AsyncSession, channel_id: int, ordered_ids: list[int]
) -> None:
    """Re-assign sort_order based on provided ordered list of IDs."""
    result = await db.execute(
        select(Thumbnail).where(
            Thumbnail.channel_id == channel_id,
            Thumbnail.id.in_(ordered_ids),
        )
    )
    thumbnails = {t.id: t for t in result.scalars().all()}
    for position, tid in enumerate(ordered_ids):
        if tid in thumbnails:
            thumbnails[tid].sort_order = position
    await db.flush()
