"""
api/routers/schedule.py
------------------------
Schedule configuration and upload history per channel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.database import get_db
from core.logging_config import get_logger
from core.security import generate_csrf_token
from models.channel import YouTubeChannel
from models.schedule_config import ScheduleConfig
from models.upload_history import UploadHistory
from models.video import Video
from scheduler.engine import add_channel_job, pause_channel_job, resume_channel_job
from scheduler.jobs import compute_next_upload_time

log = get_logger(__name__)
router = APIRouter(prefix="/schedule", tags=["Schedule"])


@router.get("", response_class=HTMLResponse)
async def schedule_page(
    request: Request,
    channel_id: int = 0,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channels_result = await db.execute(select(YouTubeChannel).where(YouTubeChannel.is_active == True))
    channels = channels_result.scalars().all()

    # Default to first channel if not specified
    if not channel_id and channels:
        channel_id = channels[0].id

    sched = history = scheduled_video = None
    channel_videos = []
    if channel_id:
        sched_result = await db.execute(
            select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
        )
        sched = sched_result.scalar_one_or_none()

        # Find the active scheduled video for this channel
        vid_res = await db.execute(
            select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
        )
        scheduled_video = vid_res.scalar_one_or_none()

        # Get all videos for this channel
        all_vids_res = await db.execute(
            select(Video).where(Video.channel_id == channel_id).order_by(Video.id.desc())
        )
        channel_videos = all_vids_res.scalars().all()

        from sqlalchemy.orm import selectinload
        hist_result = await db.execute(
            select(UploadHistory)
            .options(selectinload(UploadHistory.thumbnail))
            .where(UploadHistory.channel_id == channel_id)
            .order_by(UploadHistory.id.desc())
            .limit(50)
        )
        history = hist_result.scalars().all()

        from services.upload_progress import upload_tracker
        active_progress = None
        if scheduled_video:
            active_progress = upload_tracker.get(scheduled_video.id)
        if not active_progress and channel_id:
            active_progress = upload_tracker.get_for_channel(channel_id)

        # Do NOT treat inactive or completed past progress as an active pipeline for next schedule
        if active_progress and (not active_progress.get("active") or active_progress.get("stage") in ("success", "failed", "idle")):
            active_progress = None

        latest_waiting_release = None
        if history and history[0].status == "waiting_release":
            h = history[0]
            latest_waiting_release = {
                "history_id": h.id,
                "video_id": scheduled_video.id if scheduled_video else 0,
                "youtube_video_id": h.youtube_video_id,
                "youtube_url": h.youtube_url or (f"https://www.youtube.com/watch?v={h.youtube_video_id}" if h.youtube_video_id else None),
                "video_title": h.video_title or (scheduled_video.title if scheduled_video else "Scheduled Video"),
                "thumbnail_display_url": h.thumbnail_display_url,
                "scheduled_at": h.scheduled_at,
                "release_at": sched.next_upload_at if sched else h.scheduled_at,
                "privacy": "private",
                "send_discord": True,
            }

        # In-window catch-up: if processing time has arrived but job hasn't started yet, auto-trigger now!
        if (
            sched
            and sched.is_active
            and sched.next_process_at
            and sched.next_upload_at
            and scheduled_video
            and not active_progress
            and not latest_waiting_release
        ):
            try:
                now_utc = datetime.now(timezone.utc)
                proc_dt = datetime.fromisoformat(sched.next_process_at.replace("Z", "+00:00"))
                up_dt = datetime.fromisoformat(sched.next_upload_at.replace("Z", "+00:00"))
                if proc_dt <= now_utc < up_dt:
                    has_completed = any(
                        h.status in ("success", "completed") and h.scheduled_at == sched.next_upload_at
                        for h in (history or [])
                    )
                    if not has_completed:
                        import asyncio
                        from scheduler.jobs import preflight_process_job
                        asyncio.create_task(preflight_process_job(channel_id))
                        upload_tracker.start(scheduled_video.id, total_bytes=scheduled_video.file_size_bytes or 0, channel_id=channel_id)
                        upload_tracker.update_stage(
                            scheduled_video.id,
                            "ffmpeg",
                            "Processing window active! Starting video editing & burning overlays...",
                            5,
                        )
                        active_progress = upload_tracker.get(scheduled_video.id)
            except Exception as exc:
                log.warning("Could not auto-start in-window preflight in schedule_page", error=str(exc))

        # ── Next Thumbnail Preview ──
        from services.thumbnail import get_channel_thumbnails
        channel_thumbnails = await get_channel_thumbnails(db, channel_id)
        next_thumbnail = None

        if scheduled_video and getattr(scheduled_video, "thumbnail_id", None):
            next_thumbnail = await db.get(Thumbnail, scheduled_video.thumbnail_id)

        if not next_thumbnail and channel_thumbnails:
            mode = getattr(sched, 'thumbnail_mode', 'sequential') if sched else 'sequential'
            idx = getattr(sched, 'thumbnail_index', 0) if sched else 0
            if mode == 'random':
                next_thumbnail = channel_thumbnails[0]
            else:
                next_thumbnail = channel_thumbnails[idx % len(channel_thumbnails)]
    else:

        channel_thumbnails = []
        next_thumbnail = None

    csrf = generate_csrf_token()
    resp = templates.TemplateResponse(
        "schedule.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "selected_channel_id": channel_id,
            "schedule": sched,
            "scheduled_video": scheduled_video,
            "channel_videos": channel_videos,
            "history": history,
            "csrf_token": csrf,
            "now_utc": datetime.now(timezone.utc).isoformat(),
            "time_until_upload": sched.time_until_upload() if sched else None,
            "time_until_process": sched.time_until_process() if sched else None,
            "active_progress": active_progress,
            "latest_waiting_release": latest_waiting_release,
            "channel_thumbnails": channel_thumbnails,
            "next_thumbnail": next_thumbnail,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.post("/{channel_id}/config")
async def update_schedule(
    channel_id: int,
    timezone_str: str = Form(...),
    schedule_mode: str = Form("exact"),
    upload_hour: int = Form(14),
    upload_minute: int = Form(0),
    window_start: int = Form(14),
    window_end: int = Form(20),
    pre_process_minutes: int = Form(30),
    thumbnail_mode: str = Form("sequential"),
    selected_thumbnail_id: Optional[int] = Form(None),
    target_date: Optional[str] = Form(None),       # "today", "tomorrow", or "YYYY-MM-DD"
    custom_datetime: Optional[str] = Form(None),   # "YYYY-MM-DDTHH:MM"
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update scheduling config and reschedule the APScheduler jobs."""
    import pytz
    from scheduler.jobs import compute_next_upload_time, compute_next_process_time

    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched: Optional[ScheduleConfig] = sched_result.scalar_one_or_none()

    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Validate timezone
    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {timezone_str}")

    if schedule_mode == "window" and window_end <= window_start:
        raise HTTPException(status_code=422, detail="Window end must be after window start")

    if not (0 <= upload_hour <= 23):
        raise HTTPException(status_code=422, detail="upload_hour must be 0-23")
    if not (0 <= upload_minute <= 59):
        raise HTTPException(status_code=422, detail="upload_minute must be 0-59")

    sched.timezone = timezone_str
    sched.schedule_mode = schedule_mode
    sched.upload_hour = upload_hour
    sched.upload_minute = upload_minute
    sched.window_start_hour = window_start
    sched.window_end_hour = window_end
    sched.pre_process_minutes = pre_process_minutes
    sched.thumbnail_mode = thumbnail_mode

    if selected_thumbnail_id is not None and selected_thumbnail_id > 0:
        from services.thumbnail import get_channel_thumbnails
        pool = await get_channel_thumbnails(db, channel_id)
        for idx, t in enumerate(pool):
            if t.id == selected_thumbnail_id:
                sched.thumbnail_index = idx
                break

    # Compute and store next run times (supports today, tomorrow, and calendar dates)
    next_run = compute_next_upload_time(
        window_start, window_end, timezone_str,
        schedule_mode=schedule_mode,
        upload_hour=upload_hour,
        upload_minute=upload_minute,
        target_date=target_date,
        custom_datetime=custom_datetime,
    )
    next_process = compute_next_process_time(next_run, pre_process_minutes)
    sched.next_upload_at = next_run.isoformat()
    sched.next_process_at = next_process.isoformat()
    await db.flush()

    # Reschedule both jobs using _reschedule_channel
    from scheduler.jobs import _reschedule_channel
    _reschedule_channel(channel_id, next_run, pre_process_minutes)

    # Human-readable local times
    local_upload = next_run.astimezone(tz)
    local_process = next_process.astimezone(tz)

    return {
        "ok": True,
        "next_upload_at": sched.next_upload_at,
        "next_process_at": sched.next_process_at,
        "next_upload_local": local_upload.strftime("%Y-%m-%d %I:%M %p %Z"),
        "next_process_local": local_process.strftime("%Y-%m-%d %I:%M %p %Z"),
        "time_until_upload": sched.time_until_upload(),
        "time_until_process": sched.time_until_process(),
        "formatted_time_until_upload": sched.formatted_time_until_upload,
        "formatted_time_until_process": sched.formatted_time_until_process,
    }


@router.post("/{channel_id}/pause")
async def pause_schedule(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched = sched_result.scalar_one_or_none()
    if sched:
        sched.is_active = False
        await db.flush()
    pause_channel_job(channel_id)
    return {"ok": True}


@router.post("/{channel_id}/resume")
async def resume_schedule(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched = sched_result.scalar_one_or_none()
    if sched:
        sched.is_active = True
        await db.flush()
    resume_channel_job(channel_id)
    return {"ok": True}


@router.delete("/{channel_id}")
async def delete_schedule(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete the schedule config for a channel and cancel its APScheduler jobs."""
    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched = sched_result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=404, detail="No schedule found for this channel")

    # Cancel APScheduler jobs
    from scheduler.engine import get_scheduler
    try:
        sch = get_scheduler()
        for job_id in (f"upload_{channel_id}", f"process_{channel_id}"):
            try:
                sch.remove_job(job_id)
                log.info("Removed scheduler job", job_id=job_id)
            except Exception:
                pass
    except Exception as exc:
        log.warning("Could not cancel jobs", error=str(exc))

    await db.delete(sched)
    await db.commit()
    log.info("Schedule deleted", channel_id=channel_id)
    return {"ok": True, "message": f"Schedule for channel {channel_id} deleted"}


@router.post("/{channel_id}/create")
async def create_schedule(
    channel_id: int,
    timezone_str: str = Form("Asia/Kolkata"),
    schedule_mode: str = Form("exact"),
    upload_hour: int = Form(14),
    upload_minute: int = Form(0),
    pre_process_minutes: int = Form(30),
    thumbnail_mode: str = Form("sequential"),
    target_date: Optional[str] = Form(None),       # "today", "tomorrow", or "YYYY-MM-DD"
    custom_datetime: Optional[str] = Form(None),   # "YYYY-MM-DDTHH:MM"
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a brand-new schedule for a channel (fails if one already exists)."""
    import pytz
    from scheduler.jobs import compute_next_upload_time, compute_next_process_time
    from scheduler.engine import get_scheduler
    from scheduler.jobs import daily_upload_job, preflight_process_job

    # Check channel exists
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Check no existing schedule
    existing = (await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Schedule already exists. Delete it first.")

    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {timezone_str}")

    next_run = compute_next_upload_time(
        14, 20, timezone_str,
        schedule_mode=schedule_mode,
        upload_hour=upload_hour,
        upload_minute=upload_minute,
        target_date=target_date,
        custom_datetime=custom_datetime,
    )
    next_process = compute_next_process_time(next_run, pre_process_minutes)

    new_sched = ScheduleConfig(
        channel_id=channel_id,
        is_active=True,
        timezone=timezone_str,
        schedule_mode=schedule_mode,
        upload_hour=upload_hour,
        upload_minute=upload_minute,
        window_start_hour=14,
        window_end_hour=20,
        pre_process_minutes=pre_process_minutes,
        thumbnail_mode=thumbnail_mode,
        next_upload_at=next_run.isoformat(),
        next_process_at=next_process.isoformat(),
    )
    db.add(new_sched)
    await db.commit()

    # Schedule APScheduler jobs
    try:
        sch = get_scheduler()
        sch.add_job(daily_upload_job, trigger="date", run_date=next_run,
                    id=f"upload_{channel_id}", args=[channel_id], replace_existing=True)
        if pre_process_minutes > 0:
            sch.add_job(preflight_process_job, trigger="date", run_date=next_process,
                        id=f"process_{channel_id}", args=[channel_id], replace_existing=True)
    except Exception as exc:
        log.warning("Could not add jobs after create", error=str(exc))

    local_upload = next_run.astimezone(tz)
    return {
        "ok": True,
        "channel_id": channel_id,
        "next_upload_at": next_run.isoformat(),
        "next_process_at": next_process.isoformat(),
        "next_upload_local": local_upload.strftime("%Y-%m-%d %I:%M %p %Z"),
        "time_until_upload": new_sched.time_until_upload(),
        "time_until_process": new_sched.time_until_process(),
        "formatted_time_until_upload": new_sched.formatted_time_until_upload,
        "formatted_time_until_process": new_sched.formatted_time_until_process,
    }


@router.post("/{channel_id}/set-time")
@router.post("/{channel_id}/set-calendar-time")
async def set_calendar_schedule_time(
    channel_id: int,
    target_date: str = Form("today"),  # "today", "tomorrow", or "YYYY-MM-DD"
    upload_hour: int = Form(1),        # 0-23 (e.g. 1 for 1 AM)
    upload_minute: int = Form(0),      # 0-59
    custom_datetime: Optional[str] = Form(None),
    pre_process_minutes: Optional[int] = Form(None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Set upload schedule directly to Today (e.g. 1 AM), Tomorrow, or any specific Calendar date & time.
    Immediately reschedules the APScheduler background jobs.
    """
    import pytz
    from scheduler.jobs import compute_next_upload_time, compute_next_process_time
    from scheduler.engine import get_scheduler
    from scheduler.jobs import daily_upload_job, preflight_process_job

    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched: Optional[ScheduleConfig] = sched_result.scalar_one_or_none()

    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found for this channel")

    if not (0 <= upload_hour <= 23):
        raise HTTPException(status_code=422, detail="upload_hour must be between 0 and 23")
    if not (0 <= upload_minute <= 59):
        raise HTTPException(status_code=422, detail="upload_minute must be between 0 and 59")

    tz = pytz.timezone(sched.timezone)
    if pre_process_minutes is not None:
        sched.pre_process_minutes = pre_process_minutes

    sched.upload_hour = upload_hour
    sched.upload_minute = upload_minute
    sched.schedule_mode = "exact"

    # Compute next run time using target_date / custom_datetime
    next_run = compute_next_upload_time(
        sched.window_start_hour,
        sched.window_end_hour,
        sched.timezone,
        schedule_mode="exact",
        upload_hour=upload_hour,
        upload_minute=upload_minute,
        target_date=target_date,
        custom_datetime=custom_datetime,
    )
    next_process = compute_next_process_time(next_run, sched.pre_process_minutes)
    sched.next_upload_at = next_run.isoformat()
    sched.next_process_at = next_process.isoformat()
    await db.flush()

    # Reschedule APScheduler jobs
    try:
        sched_engine = get_scheduler()
        sched_engine.add_job(
            daily_upload_job, trigger="date", run_date=next_run,
            id=f"upload_{channel_id}", args=[channel_id], replace_existing=True,
        )
        if sched.pre_process_minutes > 0:
            sched_engine.add_job(
                preflight_process_job, trigger="date", run_date=next_process,
                id=f"process_{channel_id}", args=[channel_id], replace_existing=True,
            )
    except Exception as exc:
        log.warning("Could not reschedule jobs", error=str(exc))

    local_upload = next_run.astimezone(tz)
    local_process = next_process.astimezone(tz)

    return {
        "ok": True,
        "channel_id": channel_id,
        "target_date": target_date,
        "upload_hour": upload_hour,
        "upload_minute": upload_minute,
        "next_upload_at": sched.next_upload_at,
        "next_process_at": sched.next_process_at,
        "next_upload_local": local_upload.strftime("%Y-%m-%d %I:%M %p %Z"),
        "next_process_local": local_process.strftime("%Y-%m-%d %I:%M %p %Z"),
        "time_until_upload": sched.time_until_upload(),
        "time_until_process": sched.time_until_process(),
        "formatted_time_until_upload": sched.formatted_time_until_upload,
        "formatted_time_until_process": sched.formatted_time_until_process,
    }


@router.get("/{channel_id}/time-remaining")
async def get_time_remaining_endpoint(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return how much time is left until the next scheduled video upload and processing."""
    sched = (await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )).scalar_one_or_none()

    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found for this channel")

    return {
        "ok": True,
        "channel_id": channel_id,
        "is_active": sched.is_active,
        "next_upload_at": sched.next_upload_at,
        "next_process_at": sched.next_process_at,
        "time_until_upload": sched.time_until_upload(),
        "time_until_process": sched.time_until_process(),
        "formatted_time_until_upload": sched.formatted_time_until_upload,
        "formatted_time_until_process": sched.formatted_time_until_process,
    }


@router.get("/{channel_id}/scheduled-video")
@router.get("/{channel_id}/video")
async def get_scheduled_video_endpoint(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return details of the video scheduled to be uploaded for this channel."""
    channel = await db.get(YouTubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    sched = (await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )).scalar_one_or_none()

    active_vid = (await db.execute(
        select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
    )).scalar_one_or_none()

    if not active_vid:
        return {
            "ok": True,
            "has_video": False,
            "channel_id": channel_id,
            "channel_name": channel.name,
            "next_upload_at": sched.next_upload_at if sched else None,
            "message": "No active video configured for this schedule",
            "video": None,
        }

    return {
        "ok": True,
        "has_video": True,
        "channel_id": channel_id,
        "channel_name": channel.name,
        "next_upload_at": sched.next_upload_at if sched else None,
        "next_process_at": sched.next_process_at if sched else None,
        "video": {
            "id": active_vid.id,
            "title": active_vid.title,
            "original_filename": active_vid.original_filename,
            "file_size_bytes": active_vid.file_size_bytes,
            "file_size_mb": round(active_vid.file_size_bytes / (1024 * 1024), 2),
            "duration_seconds": active_vid.duration_seconds,
            "privacy_status": active_vid.privacy_status,
            "category_id": active_vid.category_id,
            "web_url": active_vid.web_url,
            "apply_ffmpeg": active_vid.apply_ffmpeg,
            "overlay_username_enabled": active_vid.overlay_username_enabled,
            "overlay_password_enabled": active_vid.overlay_password_enabled,
            "overlay_randomize_time": active_vid.overlay_randomize_time,
            "manage_url": f"/api/videos/{active_vid.id}/manage",
        },
    }


@router.post("/{channel_id}/set-video")
async def set_scheduled_video_endpoint(
    channel_id: int,
    video_id: int = Form(...),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set which video will be uploaded for this channel's schedule."""
    target_vid = await db.get(Video, video_id)
    if not target_vid or target_vid.channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Video not found for this channel")

    all_vids = (await db.execute(
        select(Video).where(Video.channel_id == channel_id)
    )).scalars().all()
    for v in all_vids:
        v.is_active = (v.id == video_id)

    await db.commit()
    log.info("Scheduled video updated", channel_id=channel_id, video_id=video_id, title=target_vid.title)
    return {
        "ok": True,
        "channel_id": channel_id,
        "active_video_id": video_id,
        "title": target_vid.title,
        "message": f"'{target_vid.title}' is now set as the scheduled upload video",
    }


@router.post("/{channel_id}/test")
async def test_schedule_endpoint(
    channel_id: int,
    privacy: str = Form("public"),
    lead_minutes: int = Form(5),
    send_discord: bool = Form(True),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a test run of the upload schedule: pre-uploads, then releases and announces after lead_minutes."""
    from scheduler.jobs import test_schedule_upload
    from services.youtube import UploadLimitExceededError, QuotaExceededError
    try:
        result = await test_schedule_upload(
            channel_id=channel_id,
            privacy=privacy,
            lead_minutes=lead_minutes,
            send_discord=send_discord,
        )
        return result
    except UploadLimitExceededError as exc:
        log.warning("Test schedule failed due to daily upload limit", channel_id=channel_id)
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
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "quota_exceeded",
                "message": "YouTube API Quota Exceeded (10,000 units/day project cap).",
                "resolution": "Your Google Cloud Project has consumed all quota units for today. The quota resets at midnight Pacific Time (PT).",
            },
        )
    except Exception as exc:
        log.exception("Test schedule failed", channel_id=channel_id, error=str(exc))
        err_msg = str(exc)
        if "uploadLimitExceeded" in err_msg or "exceeded the number of videos" in err_msg:
            raise HTTPException(
                status_code=429,
                detail={
                    "error_type": "upload_limit_exceeded",
                    "message": "YouTube Daily Upload Limit Exceeded: This channel has reached Google's maximum video uploads for today (24-hour limit).",
                    "resolution": "To resolve this in hosting: (1) Unlock 'Advanced features' in YouTube Studio (Settings > Channel > Feature eligibility) via 30s Video Verification or ID to unlock 100+ uploads/day, (2) Switch to another YouTube channel in the Channels tab, or (3) Wait 24 hours for Google's rolling limit to reset.",
                    "studio_url": "https://studio.youtube.com/",
                },
            )
        if "invalid_scope" in err_msg or "invalid_grant" in err_msg:
            err_msg = "YouTube OAuth error: Please reconnect this channel under 'Channels' → 'Reconnect OAuth' to refresh your Google permissions."
        raise HTTPException(status_code=500, detail=err_msg)


@router.post("/{channel_id}/release-now")
async def release_now_endpoint(
    channel_id: int,
    video_id: int = Form(...),
    youtube_video_id: str = Form(...),
    history_id: int = Form(...),
    privacy: str = Form("public"),
    send_discord: bool = Form(True),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Immediately release a pre-uploaded video to public on YouTube and post to Discord without waiting the remaining countdown."""
    from scheduler.jobs import release_video_and_announce_job
    from scheduler.engine import get_scheduler
    try:
        sch = get_scheduler()
        for jid in [f"release_scheduled_{channel_id}_{video_id}", f"release_test_{channel_id}_{video_id}"]:
            if sch.get_job(jid):
                sch.remove_job(jid)
    except Exception as exc:
        log.warning("Could not remove scheduled release job", error=str(exc))

    try:
        result = await release_video_and_announce_job(
            channel_id=channel_id,
            video_id=video_id,
            youtube_video_id=youtube_video_id,
            history_id=history_id,
            privacy=privacy,
            send_discord=send_discord,
        )
        return result
    except Exception as exc:
        log.exception("Release now failed", channel_id=channel_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))



@router.get("/{channel_id}/progress")
async def get_schedule_test_progress(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return live real-time progress of schedule test or scheduled pre-upload."""
    from services.upload_progress import upload_tracker

    vid_res = await db.execute(
        select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
    )
    video = vid_res.scalar_one_or_none()

    prog = None
    if video:
        prog = upload_tracker.get(video.id)
    if not prog:
        prog = upload_tracker.get_for_channel(channel_id)

    if prog and (prog.get("active") or prog.get("stage") == "waiting_release"):
        return prog

    # Fallback: check database for recent waiting_release or processing
    hist_res = await db.execute(
        select(UploadHistory)
        .where(UploadHistory.channel_id == channel_id)
        .order_by(UploadHistory.id.desc())
        .limit(1)
    )
    latest_hist = hist_res.scalar_one_or_none()
    if latest_hist and latest_hist.status == "waiting_release":
        sched_res = await db.execute(
            select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
        )
        sc = sched_res.scalar_one_or_none()
        v_title = latest_hist.video_title or (video.title if video else "Scheduled Video")
        v_size = video.file_size_bytes or 0 if video else 0
        v_mb = round(v_size / (1024 * 1024), 1) if v_size > 0 else 0.0
        return {
            "active": True,
            "stage": "waiting_release",
            "stage_label": "Pre-upload complete! Waiting for scheduled public release.",
            "percent": 95,
            "percent_left": 5,
            "bytes_uploaded": v_size,
            "total_bytes": v_size,
            "uploaded_mb": v_mb,
            "total_mb": v_mb,
            "remaining_mb": 0.0,
            "speed_mb": 0.0,
            "eta_seconds": 0,
            "video_id": video.id if video else 0,
            "channel_id": channel_id,
            "history_id": latest_hist.id,
            "youtube_video_id": latest_hist.youtube_video_id,
            "youtube_url": latest_hist.youtube_url or (f"https://www.youtube.com/watch?v={latest_hist.youtube_video_id}" if latest_hist.youtube_video_id else None),
            "thumbnail_display_url": latest_hist.thumbnail_display_url,
            "release_at": sc.next_upload_at if sc else latest_hist.scheduled_at,
            "video_title": v_title,
            "privacy": "private",
            "send_discord": True,
        }
    elif latest_hist and latest_hist.status in ("uploading", "processing"):
        v_title = latest_hist.video_title or (video.title if video else "Scheduled Video")
        return {
            "active": True,
            "stage": latest_hist.status,
            "stage_label": f"Processing in progress ({latest_hist.status})...",
            "percent": 50 if latest_hist.status == "uploading" else 15,
            "percent_left": 50 if latest_hist.status == "uploading" else 85,
            "video_id": video.id if video else 0,
            "channel_id": channel_id,
            "history_id": latest_hist.id,
            "video_title": v_title,
        }

    # In-window catch-up: if current time is within processing window, auto-start preflight!
    sched_res = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sc = sched_res.scalar_one_or_none()
    if sc and sc.is_active and sc.next_process_at and sc.next_upload_at and video:
        try:
            now_dt = datetime.now(timezone.utc)
            proc_dt = datetime.fromisoformat(sc.next_process_at.replace("Z", "+00:00"))
            up_dt = datetime.fromisoformat(sc.next_upload_at.replace("Z", "+00:00"))
            if proc_dt <= now_dt < up_dt:
                already_done_res = await db.execute(
                    select(UploadHistory)
                    .where(
                        UploadHistory.channel_id == channel_id,
                        UploadHistory.status.in_(["success", "completed"]),
                        UploadHistory.scheduled_at == sc.next_upload_at,
                    )
                    .limit(1)
                )
                if not already_done_res.scalar_one_or_none():
                    import asyncio
                    from scheduler.jobs import preflight_process_job
                    asyncio.create_task(preflight_process_job(channel_id))
                    upload_tracker.start(video.id, total_bytes=video.file_size_bytes or 0, channel_id=channel_id)
                    upload_tracker.update_stage(
                        video.id,
                        "ffmpeg",
                        "Processing window active! Starting video editing & burning overlays...",
                        5,
                    )
                    return upload_tracker.get(video.id)
        except Exception as exc:
            log.warning("Could not auto-start preflight in progress endpoint", error=str(exc))

    return {"active": False, "stage": "idle", "percent": 0, "percent_left": 100}


@router.post("/{channel_id}/start-preflight")
async def start_preflight_endpoint(
    channel_id: int,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trigger pre-flight video processing and YouTube pre-upload right now."""
    import asyncio
    from scheduler.jobs import preflight_process_job
    asyncio.create_task(preflight_process_job(channel_id))
    return {"ok": True, "message": "Pre-flight processing and pre-upload started!"}


@router.get("/history/api")
async def history_api(
    channel_id: int,
    page: int = 1,
    page_size: int = 20,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Paginated upload history."""
    offset = (page - 1) * page_size
    result = await db.execute(
        select(UploadHistory)
        .where(UploadHistory.channel_id == channel_id)
        .order_by(UploadHistory.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "youtube_url": r.youtube_url,
            "video_title": r.video_title,
            "scheduled_at": r.scheduled_at,
            "completed_at": r.completed_at,
            "error_message": r.error_message,
            "retry_count": r.retry_count,
        }
        for r in rows
    ]


@router.delete("/history/{history_id}")
async def delete_history_item(
    history_id: int,
    delete_from_youtube: bool = Query(True),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete an upload history record and optionally delete the video from YouTube."""
    history = await db.get(UploadHistory, history_id)
    if not history:
        raise HTTPException(status_code=404, detail="Upload history record not found")

    yt_deleted = False
    if delete_from_youtube and history.youtube_video_id:
        channel = await db.get(YouTubeChannel, history.channel_id)
        if channel and channel.is_authenticated:
            try:
                from services.youtube import build_youtube_service_from_db
                yt_svc = build_youtube_service_from_db(channel)
                yt_deleted = yt_svc.delete_video(history.youtube_video_id)
                log.info("Deleted video from YouTube", youtube_video_id=history.youtube_video_id)
            except Exception as exc:
                log.warning("Could not delete video from YouTube", error=str(exc))

    await db.delete(history)
    await db.commit()
    return {"ok": True, "history_id": history_id, "youtube_deleted": yt_deleted}


@router.delete("/youtube/{youtube_video_id}")
async def delete_youtube_video_by_id(
    youtube_video_id: str,
    channel_id: Optional[int] = Query(None),
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a video directly from YouTube by its YouTube video ID."""
    from services.youtube import build_youtube_service_from_db
    ch = None
    if channel_id:
        ch = await db.get(YouTubeChannel, channel_id)
    if not ch:
        hist = (await db.execute(select(UploadHistory).where(UploadHistory.youtube_video_id == youtube_video_id))).scalar_one_or_none()
        if hist:
            ch = await db.get(YouTubeChannel, hist.channel_id)
        if not ch:
            ch = (await db.execute(select(YouTubeChannel).where(YouTubeChannel.is_active == True))).scalar_one_or_none()

    if not ch or not ch.is_authenticated:
        raise HTTPException(status_code=400, detail="Authenticated YouTube channel not found")

    yt_svc = build_youtube_service_from_db(ch)
    try:
        yt_svc.delete_video(youtube_video_id)
        hists = (await db.execute(select(UploadHistory).where(UploadHistory.youtube_video_id == youtube_video_id))).scalars().all()
        for h in hists:
            await db.delete(h)
        await db.commit()
        return {"ok": True, "youtube_video_id": youtube_video_id, "deleted": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Thumbnail Auto-Select, Extract & Override Endpoints ─────────────────────────

@router.post("/{channel_id}/auto-select-thumbnail")
async def auto_select_thumbnail_endpoint(
    channel_id: int,
    mode: Optional[str] = Form(None),
    advance: bool = Form(True),
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """
    Auto-select or cycle to the next thumbnail from the channel pool or video.
    Advances thumbnail_index and commits to DB.
    """
    from services.thumbnail import get_channel_thumbnails
    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched = sched_result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule config not found")

    # Load scheduled video
    video_result = await db.execute(
        select(Video)
        .where(Video.channel_id == channel_id, Video.is_active == True)
        .order_by(Video.id.desc())
        .limit(1)
    )
    video = video_result.scalar_one_or_none()

    pool = await get_channel_thumbnails(db, channel_id)

    if not pool:
        return {
            "success": False,
            "detail": "No thumbnails in stock for this channel. Please upload a thumbnail in the Thumbnails tab.",
        }


    if mode in ("sequential", "random"):
        sched.thumbnail_mode = mode

    if advance:
        sched.thumbnail_index = (sched.thumbnail_index + 1) % len(pool)

    if sched.thumbnail_mode == "random":
        import random
        chosen = random.choice(pool)
    else:
        chosen = pool[sched.thumbnail_index % len(pool)]

    await db.commit()

    return {
        "success": True,
        "thumbnail": {
            "id": chosen.id,
            "filename": chosen.filename,
            "web_url": chosen.web_url,
            "width": chosen.width,
            "height": chosen.height,
            "file_size_bytes": chosen.file_size_bytes,
        },
        "pool_count": len(pool),
        "thumbnail_mode": sched.thumbnail_mode,
        "thumbnail_index": sched.thumbnail_index,
        "message": f"Auto-selected thumbnail: {chosen.filename}",
    }


@router.post("/{channel_id}/extract-thumbnail")
async def extract_thumbnail_endpoint(
    channel_id: int,
    video_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """
    Instantly extract a crisp 1280x720 HD thumbnail frame from the scheduled video.
    """
    from services.thumbnail import ensure_video_thumbnail, get_channel_thumbnails
    if video_id:
        video = await db.get(Video, video_id)
    else:
        video_result = await db.execute(
            select(Video)
            .where(Video.channel_id == channel_id, Video.is_active == True)
            .order_by(Video.id.desc())
            .limit(1)
        )
        video = video_result.scalar_one_or_none()

    if not video:
        raise HTTPException(status_code=400, detail="No video found for thumbnail extraction")

    thumb = await ensure_video_thumbnail(db, channel_id, video)
    if not thumb:
        raise HTTPException(status_code=500, detail="Failed to extract thumbnail frame from video")

    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched = sched_result.scalar_one_or_none()
    pool = await get_channel_thumbnails(db, channel_id)
    if sched and pool:
        for idx, t in enumerate(pool):
            if t.id == thumb.id:
                sched.thumbnail_index = idx
                break

    await db.commit()

    return {
        "success": True,
        "thumbnail": {
            "id": thumb.id,
            "filename": thumb.filename,
            "web_url": thumb.web_url,
            "width": thumb.width,
            "height": thumb.height,
            "file_size_bytes": thumb.file_size_bytes,
        },
        "pool_count": len(pool),
        "message": f"Successfully auto-extracted thumbnail from {video.title}!",
    }


@router.post("/{channel_id}/set-thumbnail")
async def set_active_thumbnail_endpoint(
    channel_id: int,
    thumbnail_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """
    Set a specific thumbnail as active/selected for the next upload.
    If thumbnail_id == 0, resets to auto-rotation.
    """
    from services.thumbnail import get_channel_thumbnails
    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
    )
    sched = sched_result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    pool = await get_channel_thumbnails(db, channel_id)
    chosen = None
    if thumbnail_id > 0:
        for idx, t in enumerate(pool):
            if t.id == thumbnail_id:
                sched.thumbnail_index = idx
                chosen = t
                break
        if not chosen:
            raise HTTPException(status_code=404, detail="Thumbnail not found in pool")
    else:
        # Auto mode
        if pool:
            chosen = pool[sched.thumbnail_index % len(pool)]

    await db.commit()

    return {
        "success": True,
        "thumbnail": {
            "id": chosen.id,
            "filename": chosen.filename,
            "web_url": chosen.web_url,
            "width": chosen.width,
            "height": chosen.height,
            "file_size_bytes": chosen.file_size_bytes,
        } if chosen else None,
        "pool_count": len(pool),
        "thumbnail_index": sched.thumbnail_index,
    }

