"""
scheduler/jobs.py
------------------
Core scheduler jobs:
  1. daily_upload_job  — processes + uploads video for a channel
  2. schedule_next_run — picks a random time in the configured window
  3. credential_rotation_job — rotates admin credentials
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import randint, uniform
from typing import Optional

import pytz
from sqlalchemy import select

from core.config import settings
from core.database import get_db_context
from core.logging_config import get_logger
from models.channel import YouTubeChannel
from models.schedule_config import ScheduleConfig
from models.thumbnail import Thumbnail
from models.upload_history import UploadHistory
from models.user import User
from models.video import Video
from scheduler.retry import with_exponential_backoff
from services.credential_rotator import rotate_credentials
from services.discord_notify import (
    alert_thumbnail_pool_empty,
    alert_upload_failure,
    alert_upload_limit_exceeded,
    send_upload_announcement,
)
from services.ffmpeg import process_video, probe_video
from services.thumbnail import get_next_thumbnail
from services.youtube import (
    QuotaExceededError,
    UploadLimitExceededError,
    build_youtube_service_from_db,
)

log = get_logger(__name__)

PROCESSED_DIR = Path("uploads/processed")
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


DEFAULT_RANDOM_WORDS = [
    "abc", "free", "pro", "win", "vip", "top", "star", "fast", "safe", "cool",
    "play", "hero", "mega", "best", "gold", "fire", "live", "game", "zone", "link",
    "easy", "boss", "luck", "rush", "pure", "nova", "apex", "neon", "open", "gift",
    "drop", "epic", "flow", "wave", "peak", "zoom", "power", "prime", "super", "bonus",
    "ace", "gem", "coin", "pass", "wild", "loot", "team", "club", "cast", "glow",
    "spark", "flash", "craft", "ultra", "byte", "core", "iron", "rock", "sky", "red", "blue", "xyz"
]


def generate_random_code(min_len: int = 2, max_len: int = 4, exclude: Optional[str] = None) -> str:
    """Generate a random word code (e.g. abc, free, pro, win, vip) instead of numeric digits."""
    import secrets
    words = DEFAULT_RANDOM_WORDS
    try:
        from core.config import settings
        if getattr(settings, "random_words_list", None):
            custom = [w.strip() for w in settings.random_words_list.split(",") if w.strip()]
            if custom:
                words = custom
    except Exception:
        pass

    candidates = [w for w in words if w.lower() != (exclude.lower() if exclude else "")]
    if not candidates:
        candidates = words
    return secrets.choice(candidates)


def compute_next_upload_time(
    window_start: int,
    window_end: int,
    tz_name: str,
    schedule_mode: str = "window",
    upload_hour: int = 14,
    upload_minute: int = 0,
    target_date: Optional[str] = None,       # "today", "tomorrow", "YYYY-MM-DD", or None (auto)
    custom_datetime: Optional[str] = None,   # "YYYY-MM-DDTHH:MM" ISO datetime
) -> datetime:
    """
    Compute the next upload datetime based on mode, calendar date, and time:
      - custom_datetime: exact calendar datetime (ISO format: YYYY-MM-DDTHH:MM)
      - target_date: 'today', 'tomorrow', or specific calendar date (YYYY-MM-DD)
      - 'exact': at upload_hour:upload_minute on target_date (or today if still in future, else tomorrow)
      - 'window': random minute within [window_start, window_end) on target_date
    Always returned as UTC datetime.
    """
    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz)

    # 1. Direct custom datetime (e.g. from datetime-local input)
    if custom_datetime:
        clean_dt = custom_datetime.strip().replace(" ", "T")
        try:
            if len(clean_dt) == 16:  # YYYY-MM-DDTHH:MM
                dt_obj = datetime.strptime(clean_dt, "%Y-%m-%dT%H:%M")
            else:
                dt_obj = datetime.fromisoformat(clean_dt)
            if dt_obj.tzinfo is None:
                local_upload_time = tz.localize(dt_obj)
            else:
                local_upload_time = dt_obj.astimezone(tz)
            return local_upload_time.astimezone(timezone.utc)
        except Exception as exc:
            log.warning("Could not parse custom_datetime, falling back", error=str(exc))

    # 2. Determine base local date (calendar)
    base_date = None
    if target_date == "today":
        base_date = now_local.date()
    elif target_date == "tomorrow":
        base_date = (now_local + timedelta(days=1)).date()
    elif target_date and target_date not in ("auto", "none", "null", ""):
        try:
            base_date = datetime.strptime(target_date[:10], "%Y-%m-%d").date()
        except Exception:
            base_date = None

    if schedule_mode in ("exact", "calendar"):
        if base_date:
            naive_target = datetime.combine(base_date, datetime.min.time()).replace(
                hour=upload_hour, minute=upload_minute, second=0, microsecond=0
            )
            local_upload_time = tz.localize(naive_target)
            # If target_date was explicitly "today" and the time has already passed today:
            if target_date == "today" and local_upload_time <= now_local:
                tomorrow_date = (now_local + timedelta(days=1)).date()
                local_upload_time = tz.localize(
                    datetime.combine(tomorrow_date, datetime.min.time()).replace(
                        hour=upload_hour, minute=upload_minute, second=0, microsecond=0
                    )
                )
        else:
            # Auto: check if today at upload_hour:upload_minute is still in the future!
            today_candidate = tz.localize(
                datetime.combine(now_local.date(), datetime.min.time()).replace(
                    hour=upload_hour, minute=upload_minute, second=0, microsecond=0
                )
            )
            if today_candidate > now_local:
                local_upload_time = today_candidate
            else:
                tomorrow_date = (now_local + timedelta(days=1)).date()
                local_upload_time = tz.localize(
                    datetime.combine(tomorrow_date, datetime.min.time()).replace(
                        hour=upload_hour, minute=upload_minute, second=0, microsecond=0
                    )
                )
    else:
        # Window mode
        random_hour = randint(window_start, window_end - 1)
        random_minute = randint(0, 59)
        if base_date:
            local_upload_time = tz.localize(
                datetime.combine(base_date, datetime.min.time()).replace(
                    hour=random_hour, minute=random_minute, second=0, microsecond=0
                )
            )
        else:
            today_candidate = tz.localize(
                datetime.combine(now_local.date(), datetime.min.time()).replace(
                    hour=random_hour, minute=random_minute, second=0, microsecond=0
                )
            )
            if today_candidate > now_local:
                local_upload_time = today_candidate
            else:
                tomorrow_date = (now_local + timedelta(days=1)).date()
                local_upload_time = tz.localize(
                    datetime.combine(tomorrow_date, datetime.min.time()).replace(
                        hour=random_hour, minute=random_minute, second=0, microsecond=0
                    )
                )

    return local_upload_time.astimezone(timezone.utc)


def compute_next_process_time(
    upload_utc: datetime,
    pre_process_minutes: int = 30,
) -> datetime:
    """
    Compute the video pre-processing trigger time:
      pre_process_minutes before the scheduled upload time.
    Returns UTC datetime.
    """
    if pre_process_minutes <= 0:
        return upload_utc
    proc = upload_utc - timedelta(minutes=pre_process_minutes)
    now_utc = datetime.now(timezone.utc)
    # If the computed process time is already in the past but upload is in the future,
    # start processing in 5 seconds so pre-processing runs properly
    if proc <= now_utc < upload_utc:
        return now_utc + timedelta(seconds=5)
    return proc


# ── Main upload job ──────────────────────────────────────────────────────────

async def daily_upload_job(channel_id: int) -> None:
    """
    Full pipeline for one channel's daily upload:
      1. Load config from DB
      2. FFmpeg processing
      3. Thumbnail selection
      4. YouTube upload (with retry)
      5. Discord announcement
      6. Schedule next run
    """
    log.info("Starting daily upload job", channel_id=channel_id)
    start = time.monotonic()

    async with get_db_context() as db:
        # ── Load channel ─────────────────────────────────────────────────
        channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, channel_id)
        if not channel or not channel.is_active or not channel.is_authenticated:
            log.warning("Channel not ready for upload", channel_id=channel_id)
            return

        # ── Load active video ────────────────────────────────────────────
        result = await db.execute(
            select(Video).where(
                Video.channel_id == channel_id,
                Video.is_active == True,
            )
        )
        video: Optional[Video] = result.scalar_one_or_none()
        if not video:
            log.error("No active video for channel", channel_id=channel_id)
            await alert_upload_failure(channel.name, "No active video configured", 0)
            return

        # ── Load schedule config ─────────────────────────────────────────
        sched_result = await db.execute(
            select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
        )
        sched: Optional[ScheduleConfig] = sched_result.scalar_one_or_none()
        if not sched or not sched.is_active:
            log.info("Schedule paused or missing", channel_id=channel_id)
            return

        # ── Check if this schedule target run has already been released ───
        already_done_res = await db.execute(
            select(UploadHistory)
            .where(
                UploadHistory.channel_id == channel_id,
                UploadHistory.status.in_(["success", "completed"]),
                UploadHistory.scheduled_at == sched.next_upload_at,
            )
            .order_by(UploadHistory.id.desc())
            .limit(1)
        )
        already_done = already_done_res.scalar_one_or_none()
        if already_done:
            log.info(
                "Upload for this schedule target has already completed/released. Advancing to next scheduled run.",
                channel_id=channel_id,
                scheduled_at=sched.next_upload_at,
            )
            next_time = compute_next_upload_time(
                sched.window_start_hour,
                sched.window_end_hour,
                sched.timezone,
                schedule_mode=getattr(sched, 'schedule_mode', 'exact'),
                upload_hour=getattr(sched, 'upload_hour', 14),
                upload_minute=getattr(sched, 'upload_minute', 0),
            )
            sched.next_upload_at = next_time.isoformat()
            pre_proc_min = getattr(sched, 'pre_process_minutes', 30)
            process_time = compute_next_process_time(next_time, pre_proc_min)
            sched.next_process_at = process_time.isoformat()
            _reschedule_channel(channel_id, next_time, pre_proc_min)
            await db.commit()
            return

        # ── Check for pre-uploaded video in waiting_release state ────────
        wait_hist_res = await db.execute(
            select(UploadHistory)
            .where(UploadHistory.channel_id == channel_id, UploadHistory.status == "waiting_release")
            .order_by(UploadHistory.id.desc())
            .limit(1)
        )
        waiting_hist = wait_hist_res.scalar_one_or_none()
        if waiting_hist and waiting_hist.youtube_video_id:
            log.info(
                "Found pre-uploaded video waiting for release. Releasing now!",
                channel_id=channel_id,
                youtube_video_id=waiting_hist.youtube_video_id,
            )
            # Cancel any existing scheduled release job to prevent double-firing
            try:
                from scheduler.engine import get_scheduler
                sch = get_scheduler()
                for jid in [f"release_scheduled_{channel_id}_{video.id}", f"release_test_{channel_id}_{video.id}"]:
                    if sch.get_job(jid):
                        sch.remove_job(jid)
                        log.info("Cancelled duplicate release job before run_scheduled_upload release", job_id=jid)
            except Exception as exc:
                log.debug("Scheduler job cleanup notice in scheduled upload", error=str(exc))

            privacy = getattr(video, "privacy_status", "public") or "public"
            await release_video_and_announce_job(
                channel_id=channel_id,
                video_id=video.id,
                youtube_video_id=waiting_hist.youtube_video_id,
                history_id=waiting_hist.id,
                privacy=privacy,
                send_discord=True,
            )
            # Reschedule next run
            next_time = compute_next_upload_time(
                sched.window_start_hour,
                sched.window_end_hour,
                sched.timezone,
                schedule_mode=getattr(sched, 'schedule_mode', 'exact'),
                upload_hour=getattr(sched, 'upload_hour', 14),
                upload_minute=getattr(sched, 'upload_minute', 0),
            )
            sched.next_upload_at = next_time.isoformat()
            pre_proc_min = getattr(sched, 'pre_process_minutes', 30)
            process_time = compute_next_process_time(next_time, pre_proc_min)
            sched.next_process_at = process_time.isoformat()
            _reschedule_channel(channel_id, next_time, pre_proc_min)
            await db.commit()
            return


async def prepare_scheduled_thumbnail(
    db: AsyncSession,
    channel_id: int,
    sched: Optional[ScheduleConfig],
    video: Video,
) -> Optional[Thumbnail]:
    """
    Select and standardize the next thumbnail for upload,
    guaranteeing YouTube working size: 1280x720, < 2MB JPEG.
    Uses video's explicitly assigned thumbnail, or rotates through channel stock pool.
    """
    from services.thumbnail import get_next_thumbnail, standardize_thumbnail

    thumbnail = None

    # 1. Check if video has an assigned thumbnail
    if getattr(video, "thumbnail_id", None):
        t_rec = await db.get(Thumbnail, video.thumbnail_id)
        if t_rec and t_rec.is_active and not t_rec.filename.startswith("auto_video_"):
            thumbnail = t_rec

    # 2. If not, get from channel stock pool
    if not thumbnail:
        thumb_mode = sched.thumbnail_mode if sched else "sequential"
        thumb_idx = sched.thumbnail_index if sched else 0

        thumb_result = await get_next_thumbnail(
            db=db,
            channel_id=channel_id,
            mode=thumb_mode,
            current_index=thumb_idx,
            video=video,
        )
        if thumb_result:
            thumbnail, new_thumb_index = thumb_result
            if sched:
                sched.thumbnail_index = new_thumb_index + 1

    if thumbnail and thumbnail.stored_path and os.path.exists(thumbnail.stored_path):
        try:
            w, h, sz = standardize_thumbnail(thumbnail.stored_path, target_width=1280, target_height=720)
            thumbnail.width = w
            thumbnail.height = h
            thumbnail.file_size_bytes = sz
            await db.flush()
        except Exception as st_err:
            log.warning("Could not standardize thumbnail", thumb_id=thumbnail.id, error=str(st_err))

    return thumbnail



async def run_scheduled_upload(channel_id: int) -> None:
    """Execute a scheduled video upload for a channel."""
    async with AsyncSessionLocal() as db:
        # Load channel with token refresh
        channel = await _get_authenticated_channel(db, channel_id)
        if not channel:
            log.warning("Channel not found or inactive", channel_id=channel_id)
            return

        # Load active video
        video = await _get_active_video(db, channel_id)
        if not video:
            log.warning("No active video found for upload", channel_id=channel_id)
            return

        # Load schedule config
        sched_res = await db.execute(
            select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
        )
        sched = sched_res.scalar_one_or_none()
        if not sched or not sched.is_active:
            log.info("Schedule not active for channel", channel_id=channel_id)
            return

        # Check if already pre-uploaded beforehand by preflight job
        existing_res = await db.execute(
            select(UploadHistory).where(
                UploadHistory.channel_id == channel_id,
                UploadHistory.status == "waiting_release",
                UploadHistory.youtube_video_id.isnot(None),
            ).order_by(UploadHistory.id.desc()).limit(1)
        )
        existing_waiting = existing_res.scalar_one_or_none()
        if existing_waiting and existing_waiting.youtube_video_id:
            log.info(
                "Video already pre-uploaded to YouTube! Directly executing public release now.",
                channel_id=channel_id,
                video_id=existing_waiting.youtube_video_id,
                history_id=existing_waiting.id,
            )
            privacy = getattr(video, "privacy_status", "public") or "public"
            await execute_release_job(
                channel_id=channel_id,
                history_id=existing_waiting.id,
                youtube_video_id=existing_waiting.youtube_video_id,
                privacy=privacy,
                send_discord=True,
            )
            # Reschedule next run
            next_time = compute_next_upload_time(
                sched.window_start_hour,
                sched.window_end_hour,
                sched.timezone,
                schedule_mode=getattr(sched, 'schedule_mode', 'exact'),
                upload_hour=getattr(sched, 'upload_hour', 14),
                upload_minute=getattr(sched, 'upload_minute', 0),
            )
            sched.next_upload_at = next_time.isoformat()
            pre_proc_min = getattr(sched, 'pre_process_minutes', 30)
            process_time = compute_next_process_time(next_time, pre_proc_min)
            sched.next_process_at = process_time.isoformat()
            _reschedule_channel(channel_id, next_time, pre_proc_min)
            await db.commit()
            return

        from services.upload_progress import upload_tracker
        upload_tracker.start(video.id, total_bytes=video.file_size_bytes or 0, channel_id=channel.id)

        # ── Step 1: Thumbnail Selection & Standardization ────────────────
        upload_tracker.update_stage(video.id, "thumbnail", "Auto-selecting & preparing thumbnail...", 5)
        thumbnail = await prepare_scheduled_thumbnail(db, channel_id, sched, video)
        if not thumbnail:
            await alert_thumbnail_pool_empty(channel.name)

        if thumbnail:
            upload_tracker.update_metadata(video.id, {
                "thumbnail_id": thumbnail.id,
                "thumbnail_display_url": thumbnail.web_url,
                "thumbnail_filename": thumbnail.filename,
            })
            upload_tracker.update_stage(video.id, "thumbnail", f"Prepared thumbnail: {thumbnail.filename} (1280×720)", 8)

        # ── History record ───────────────────────────────────────────────
        history = UploadHistory(
            channel_id=channel_id,
            status="processing",
            video_title=video.title,
            thumbnail_id=thumbnail.id if thumbnail else None,
            scheduled_at=sched.next_upload_at,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(history)
        await db.flush()

        try:
            # ── Step 2: Video pre-processing & Thumbnail embedding (FFmpeg)
            output_path = str(PROCESSED_DIR / f"ch{channel_id}_{history.id}.mp4")
            t0 = time.monotonic()
            thumb_path = thumbnail.stored_path if (thumbnail and thumbnail.stored_path and os.path.exists(thumbnail.stored_path)) else None
            should_process = video.apply_ffmpeg or (thumb_path is not None)

            if should_process:
                overlays = []
                if video.apply_ffmpeg:
                    upload_tracker.update_stage(video.id, "ffmpeg", "Applying FFmpeg Text Overlays & Burns...", 10)
                    if getattr(video, "overlay_randomize_username", True):
                        cur_user = generate_random_code()
                    else:
                        admin_res = await db.execute(select(User).where(User.id == 1))
                        admin_user = admin_res.scalar_one_or_none()
                        cur_user = admin_user.username if admin_user else settings.admin_username

                    if getattr(video, "overlay_randomize_password", True):
                        cur_pwd = generate_random_code(exclude=cur_user)
                    else:
                        cur_pwd = settings.admin_password[:4] if len(settings.admin_password) >= 4 else (settings.admin_password + "99")[:4]

                    # Auto-create user on LicenseAuth / KeyAuth if enabled
                    try:
                        from services.licenseauth import auto_create_user_for_video
                        la_res = await auto_create_user_for_video(cur_user, cur_pwd, db=db)
                        if la_res.get("success"):
                            log.info("LicenseAuth user auto-created successfully", user=cur_user)
                        elif not la_res.get("skipped"):
                            log.warning("LicenseAuth user auto-creation returned error", user=cur_user, result=la_res)
                    except Exception as _la_err:
                        log.warning("LicenseAuth user auto-creation error", error=str(_la_err))

                    # Calculate timestamps
                    u_start = video.overlay_username_start or 25.0
                    u_end = video.overlay_username_end or 35.0
                    p_start = video.overlay_password_start or 25.0
                    p_end = video.overlay_password_end or 35.0

                    if video.overlay_randomize_time:
                        min_t = video.overlay_random_p_min if video.overlay_random_p_min is not None else 25.0
                        max_t = video.overlay_random_p_max if video.overlay_random_p_max is not None else 60.0
                        if max_t > min_t:
                            dur = max(2.0, (video.overlay_password_end or 35.0) - (video.overlay_password_start or 25.0))
                            upper = max(min_t, max_t - dur)
                            shared_start = round(uniform(min_t, upper), 1)
                            shared_end = round(shared_start + dur, 1)
                            u_start = shared_start; u_end = shared_end
                            p_start = shared_start; p_end = shared_end

                    if video.overlay_username_enabled and video.overlay_username_text:
                        u_txt = video.overlay_username_text.replace("{username}", cur_user)
                        overlays.append({
                            "enabled": True,
                            "text": u_txt,
                            "start": u_start, "end": u_end,
                            "position": video.overlay_username_position or "top-left",
                            "fontsize": video.overlay_username_font_size or 36,
                            "color": video.overlay_username_color or "#ffffff",
                        })

                    if video.overlay_password_enabled and video.overlay_password_text:
                        p_txt = video.overlay_password_text.replace("{password}", cur_pwd)
                        overlays.append({
                            "enabled": True,
                            "text": p_txt,
                            "start": p_start, "end": p_end,
                            "position": video.overlay_password_position or "bottom-right",
                            "fontsize": video.overlay_password_font_size or 36,
                            "color": video.overlay_password_color or "#22c55e",
                        })

                    upload_tracker.update_stage(
                        video.id,
                        "ffmpeg",
                        f"Editing video: Burning User [{cur_user}] & Pass [{cur_pwd}] at {int(u_start)}s-{int(u_end)}s...",
                        10,
                    )
                    upload_tracker.update_metadata(video.id, {
                        "overlay_username": cur_user,
                        "overlay_password": cur_pwd,
                        "overlay_burn_time": f"{int(u_start)}s - {int(u_end)}s",
                        "edit_details": f"User: {cur_user} • Pass: {cur_pwd}",
                    })

                await process_video(
                    input_path=video.stored_path,
                    output_path=output_path,
                    trim_start=video.trim_start if video.apply_ffmpeg else None,
                    trim_end=video.trim_end if video.apply_ffmpeg else None,
                    intro_path=video.intro_path if video.apply_ffmpeg else None,
                    outro_path=video.outro_path if video.apply_ffmpeg else None,
                    overlays=overlays if video.apply_ffmpeg else None,
                    thumbnail_path=thumb_path,
                    thumbnail_intro_duration=1.0,
                )
            else:
                # Use original
                output_path = video.stored_path

            history.processing_duration = time.monotonic() - t0

            # ── Step 3: YouTube upload ───────────────────────────────────
            import json as _json
            tags = _json.loads(video.tags) if video.tags else []

            yt_svc = build_youtube_service_from_db(channel)
            history.status = "uploading"
            await db.flush()

            def on_progress(bytes_uploaded, total_bytes):
                upload_tracker.update_bytes(video.id, bytes_uploaded, total_bytes)

            upload_tracker.update_stage(video.id, "uploading", "Connecting & streaming video to YouTube...", 15)

            t1 = time.monotonic()
            video_id = await with_exponential_backoff(
                _run_upload,
                yt_svc,
                output_path,
                video.title,
                video.description,
                tags,
                video.category_id,
                video.privacy_status,
                progress_callback=on_progress,
                max_retries=5,
                base_delay=30.0,
                retriable_exceptions=(Exception,),
            )
            history.upload_duration = time.monotonic() - t1

            # ── Step 4: Thumbnail attachment ─────────────────────────────
            if thumbnail and thumbnail.stored_path:
                upload_tracker.update_stage(video.id, "finalizing", "Setting video thumbnail on YouTube (1s photo)...", 95)
                try:
                    yt_svc.set_thumbnail(video_id, thumbnail.stored_path)
                    log.info("Thumbnail successfully attached to video", video_id=video_id, path=thumbnail.stored_path)
                except QuotaExceededError:
                    log.warning("Quota exceeded for thumbnail", video_id=video_id)
                except Exception as exc:
                    log.warning("Thumbnail set failed", video_id=video_id, error=str(exc))
                    history.error_message = (history.error_message or "") + f" [Thumbnail warning: {exc}]"

            # Update token storage after potential refresh
            token_data = yt_svc.get_encrypted_tokens()
            channel.encrypted_access_token = token_data["encrypted_access_token"]
            channel.encrypted_refresh_token = token_data["encrypted_refresh_token"]
            channel.token_expiry = token_data["token_expiry"]

            # ── Step 5: History update ───────────────────────────────────
            yt_url = f"https://www.youtube.com/watch?v={video_id}"
            history.youtube_video_id = video_id
            history.youtube_url = yt_url
            history.status = "success"
            history.completed_at = datetime.now(timezone.utc).isoformat()

            # ── Step 6: Discord announcement ─────────────────────────────
            try:
                discord_msg_id = await send_upload_announcement(
                    channel_id=channel_id,
                    video_title=video.title,
                    youtube_url=yt_url,
                    thumbnail_path=thumbnail.stored_path if thumbnail else None,
                    channel_name=channel.name,
                )
                if discord_msg_id:
                    history.discord_message_id = str(discord_msg_id)
                    await db.commit()
            except Exception as exc:
                log.warning("Could not send Discord announcement for scheduled upload", error=str(exc))

            # ── Auto-comment trigger ─────────────────────────────────────
            try:
                from services.comment_poster import trigger_auto_comment
                await trigger_auto_comment(
                    channel_id=channel_id,
                    video_id=video.id,
                    youtube_video_id=video_id,
                    video_title=video.title,
                )
            except Exception as exc:
                log.warning("Could not trigger auto-comment for scheduled upload", error=str(exc))

            # ── Step 7: Schedule next run ─────────────────────────────────
            next_time = compute_next_upload_time(
                sched.window_start_hour,
                sched.window_end_hour,
                sched.timezone,
                schedule_mode=getattr(sched, 'schedule_mode', 'exact'),
                upload_hour=getattr(sched, 'upload_hour', 14),
                upload_minute=getattr(sched, 'upload_minute', 0),
            )
            sched.next_upload_at = next_time.isoformat()
            pre_proc_min = getattr(sched, 'pre_process_minutes', 30)
            process_time = compute_next_process_time(next_time, pre_proc_min)
            sched.next_process_at = process_time.isoformat()
            _reschedule_channel(channel_id, next_time, pre_proc_min)

            log.info(
                "Upload job complete",
                channel_id=channel_id,
                video_id=video_id,
                total_seconds=round(time.monotonic() - start, 1),
                next_upload=sched.next_upload_at,
            )

        except UploadLimitExceededError as exc:
            history.status = "failed"
            history.error_message = (
                "YouTube Daily Upload Limit Exceeded: Channel reached Google's 24h upload limit. "
                "Enable Advanced Features in YouTube Studio or switch channels."
            )
            history.completed_at = datetime.now(timezone.utc).isoformat()
            history.retry_count = 0
            await db.commit()
            await alert_upload_limit_exceeded(channel.name)
            log.error("YouTube daily upload limit reached for channel", channel_id=channel_id)

        except QuotaExceededError as exc:
            history.status = "failed"
            history.error_message = f"YouTube quota exceeded (10,000 units/day project cap): {exc}"
            history.completed_at = datetime.now(timezone.utc).isoformat()
            history.retry_count += 1
            await db.commit()
            await alert_upload_failure(channel.name, str(exc), history.retry_count)
            log.error("Quota exceeded", channel_id=channel_id)

        except Exception as exc:
            err_str = str(exc)
            if "uploadLimitExceeded" in err_str or "exceeded the number of videos" in err_str:
                history.status = "failed"
                history.error_message = (
                    "YouTube Daily Upload Limit Exceeded: Channel reached Google's 24h upload limit. "
                    "Enable Advanced Features in YouTube Studio or switch channels."
                )
                history.completed_at = datetime.now(timezone.utc).isoformat()
                history.retry_count = 0
                await db.commit()
                await alert_upload_limit_exceeded(channel.name)
                log.error("YouTube daily upload limit reached for channel", channel_id=channel_id)
            else:
                history.status = "failed"
                history.error_message = err_str[:1000]
                history.completed_at = datetime.now(timezone.utc).isoformat()
                history.retry_count += 1
                await db.commit()
                await alert_upload_failure(channel.name, str(exc), history.retry_count)
                log.exception("Upload job failed", channel_id=channel_id, error=str(exc))
            raise  # let APScheduler see the failure


async def _run_upload(yt_svc, output_path, title, description, tags, category_id, privacy_status, progress_callback=None):
    """Thin async wrapper for the blocking YouTube upload call."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: yt_svc.upload_video(
            video_path=output_path,
            title=title,
            description=description,
            tags=tags,
            category_id=category_id,
            privacy_status=privacy_status,
            progress_callback=progress_callback,
        ),
    )


_active_preflights: set[int] = set()


def _reschedule_channel(channel_id: int, next_upload: datetime, pre_process_minutes: int = 30) -> None:
    """Update the APScheduler jobs for this channel.

    Two jobs are managed:
      - 'process_{channel_id}': fires pre_process_minutes before upload (FFmpeg + prep)
      - 'upload_{channel_id}': fires at the scheduled upload time (YT upload)
    If pre_process_minutes <= 0 the process job is skipped and upload runs at next_upload.
    """
    from scheduler.engine import get_scheduler
    sched = get_scheduler()
    now_utc = datetime.now(timezone.utc)

    # Schedule the actual upload job
    upload_job_id = f"upload_{channel_id}"
    try:
        sched.reschedule_job(upload_job_id, trigger="date", run_date=next_upload)
    except Exception:
        try:
            sched.add_job(
                daily_upload_job,
                trigger="date",
                run_date=next_upload,
                id=upload_job_id,
                args=[channel_id],
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except Exception as exc:
            log.warning("Could not schedule upload job", job_id=upload_job_id, error=str(exc))

    # Schedule the pre-processing job
    if pre_process_minutes > 0:
        process_time = compute_next_process_time(next_upload, pre_process_minutes)
        process_job_id = f"process_{channel_id}"

        # If process_time is already in the past or right now, but upload target is in the future:
        # We are ALREADY inside the processing window! Fire immediately!
        if process_time <= now_utc < next_upload:
            log.info("Already within pre-processing window; scheduling preflight job immediately", channel_id=channel_id)
            try:
                sched.add_job(
                    preflight_process_job,
                    trigger="date",
                    run_date=now_utc + timedelta(seconds=2),
                    id=process_job_id,
                    args=[channel_id],
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
            except Exception as exc:
                log.warning("Could not schedule immediate preflight job", job_id=process_job_id, error=str(exc))
        elif process_time > now_utc:
            try:
                sched.reschedule_job(process_job_id, trigger="date", run_date=process_time)
            except Exception:
                try:
                    sched.add_job(
                        preflight_process_job,
                        trigger="date",
                        run_date=process_time,
                        id=process_job_id,
                        args=[channel_id],
                        replace_existing=True,
                        misfire_grace_time=3600,
                    )
                except Exception as exc:
                    log.warning("Could not schedule process job", job_id=process_job_id, error=str(exc))


# ── Pre-flight processing job ────────────────────────────────────────────────

async def preflight_process_job(channel_id: int) -> None:
    """
    Fires pre_process_minutes before the scheduled upload time.
    Pre-processes the video with FFmpeg, burns dynamic username & password overlays,
    attaches thumbnail, and pre-uploads to YouTube beforehand as Private.
    At scheduled upload time, flips to Public and announces on Discord.
    """
    if channel_id in _active_preflights:
        log.info("Pre-flight processing already running for channel", channel_id=channel_id)
        return
    _active_preflights.add(channel_id)

    try:
        log.info("Starting pre-flight video processing", channel_id=channel_id)

        async with get_db_context() as db:
            channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, channel_id)
            if not channel or not channel.is_active:
                log.warning("Channel not ready for pre-flight processing", channel_id=channel_id)
                return

            result = await db.execute(
                select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
            )
            video: Optional[Video] = result.scalar_one_or_none()
            if not video:
                log.warning("No active video for pre-flight processing", channel_id=channel_id)
                return

            from services.upload_progress import upload_tracker

            if not channel.is_authenticated:
                err = f"Channel '{channel.name}' is not authenticated with YouTube. Please authorize via Channels page."
                log.warning(err, channel_id=channel_id)
                upload_tracker.start(video.id, total_bytes=video.file_size_bytes or 0, channel_id=channel.id)
                upload_tracker.fail(video.id, err)
                return

            if not video.stored_path or not os.path.exists(video.stored_path):
                err = f"Video file not found at: {video.stored_path}"
                log.error(err, channel_id=channel_id, video_id=video.id)
                upload_tracker.start(video.id, total_bytes=video.file_size_bytes or 0, channel_id=channel.id)
                upload_tracker.fail(video.id, err)
                return

            sched_result = await db.execute(
                select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
            )
            sched: Optional[ScheduleConfig] = sched_result.scalar_one_or_none()
            if not sched or not sched.is_active:
                log.info("Schedule paused, skipping pre-flight", channel_id=channel_id)
                return

            # Check if this schedule target run is already waiting_release or completed
            existing_hist_res = await db.execute(
                select(UploadHistory)
                .where(
                    UploadHistory.channel_id == channel_id,
                    UploadHistory.status.in_(["waiting_release", "success", "completed"]),
                    UploadHistory.scheduled_at == sched.next_upload_at,
                )
                .limit(1)
            )
            if existing_hist_res.scalar_one_or_none():
                log.info("Video for this scheduled run is already pre-uploaded or completed", channel_id=channel_id)
                return

            upload_tracker.start(video.id, total_bytes=video.file_size_bytes or 0, channel_id=channel.id)

            try:
                # ── Thumbnail Selection & Standardization ───────────────────
                upload_tracker.update_stage(video.id, "thumbnail", "Auto-selecting & preparing thumbnail...", 4)
                thumbnail = await prepare_scheduled_thumbnail(db, channel_id, sched, video)
                if thumbnail:
                    upload_tracker.update_metadata(video.id, {
                        "thumbnail_id": thumbnail.id,
                        "thumbnail_display_url": thumbnail.web_url,
                        "thumbnail_filename": thumbnail.filename,
                    })
                    upload_tracker.update_stage(
                        video.id, "thumbnail", f"Prepared thumbnail: {thumbnail.filename} (1280×720)", 6
                    )

                output_path = str(PROCESSED_DIR / f"preflight_ch{channel_id}.mp4")
                thumb_path = thumbnail.stored_path if (thumbnail and thumbnail.stored_path and os.path.exists(thumbnail.stored_path)) else None
                should_process = video.apply_ffmpeg or (thumb_path is not None)

                if should_process:
                    overlays = []
                    if video.apply_ffmpeg:
                        upload_tracker.update_stage(video.id, "ffmpeg", "Applying FFmpeg Text Overlays & Burns...", 8)

                        if getattr(video, "overlay_randomize_username", True):
                            cur_user = generate_random_code()
                        else:
                            admin_res = await db.execute(select(User).where(User.id == 1))
                            admin_user = admin_res.scalar_one_or_none()
                            cur_user = admin_user.username if admin_user else settings.admin_username

                        if getattr(video, "overlay_randomize_password", True):
                            cur_pwd = generate_random_code(exclude=cur_user)
                        else:
                            cur_pwd = settings.admin_password[:4] if len(settings.admin_password) >= 4 else (settings.admin_password + "99")[:4]

                        # Auto-create user on LicenseAuth / KeyAuth if enabled
                        try:
                            from services.licenseauth import auto_create_user_for_video
                            la_res = await auto_create_user_for_video(cur_user, cur_pwd, db=db)
                            if la_res.get("success"):
                                log.info("LicenseAuth preflight user auto-created successfully", user=cur_user)
                            elif not la_res.get("skipped"):
                                log.warning("LicenseAuth preflight user auto-creation returned error", user=cur_user, result=la_res)
                        except Exception as _la_err:
                            log.warning("LicenseAuth preflight user auto-creation error", error=str(_la_err))

                        u_start = video.overlay_username_start or 25.0
                        u_end = video.overlay_username_end or 35.0
                        p_start = video.overlay_password_start or 25.0
                        p_end = video.overlay_password_end or 35.0

                        if video.overlay_randomize_time:
                            min_t = video.overlay_random_p_min if video.overlay_random_p_min is not None else 25.0
                            max_t = video.overlay_random_p_max if video.overlay_random_p_max is not None else 60.0
                            if max_t > min_t:
                                dur = max(2.0, (video.overlay_password_end or 35.0) - (video.overlay_password_start or 25.0))
                                upper = max(min_t, max_t - dur)
                                shared_start = round(uniform(min_t, upper), 1)
                                shared_end = round(shared_start + dur, 1)
                                u_start = shared_start; u_end = shared_end
                                p_start = shared_start; p_end = shared_end

                        if video.overlay_username_enabled and video.overlay_username_text:
                            overlays.append({
                                "enabled": True,
                                "text": video.overlay_username_text.replace("{username}", cur_user),
                                "start": u_start, "end": u_end,
                                "position": video.overlay_username_position or "top-left",
                                "fontsize": video.overlay_username_font_size or 36,
                                "color": video.overlay_username_color or "#ffffff",
                            })
                        if video.overlay_password_enabled and video.overlay_password_text:
                            overlays.append({
                                "enabled": True,
                                "text": video.overlay_password_text.replace("{password}", cur_pwd),
                                "start": p_start, "end": p_end,
                                "position": video.overlay_password_position or "bottom-right",
                                "fontsize": video.overlay_password_font_size or 36,
                                "color": video.overlay_password_color or "#22c55e",
                            })

                        upload_tracker.update_stage(
                            video.id,
                            "ffmpeg",
                            f"Editing video: Burning User [{cur_user}] & Pass [{cur_pwd}] at {int(u_start)}s-{int(u_end)}s...",
                            10,
                        )
                        upload_tracker.update_metadata(video.id, {
                            "overlay_username": cur_user,
                            "overlay_password": cur_pwd,
                            "overlay_burn_time": f"{int(u_start)}s - {int(u_end)}s",
                            "edit_details": f"User: {cur_user} • Pass: {cur_pwd}",
                        })
                    else:
                        upload_tracker.update_stage(video.id, "ffmpeg", "Embedding thumbnail into video intro...", 10)

                    await process_video(
                        input_path=video.stored_path,
                        output_path=output_path,
                        trim_start=video.trim_start if video.apply_ffmpeg else None,
                        trim_end=video.trim_end if video.apply_ffmpeg else None,
                        intro_path=video.intro_path if video.apply_ffmpeg else None,
                        outro_path=video.outro_path if video.apply_ffmpeg else None,
                        overlays=overlays if video.apply_ffmpeg else None,
                        thumbnail_path=thumb_path,
                        thumbnail_intro_duration=1.0,
                    )
                else:
                    output_path = video.stored_path
                    upload_tracker.update_stage(video.id, "ffmpeg", "Using original video (overlays off)...", 10)

                # ── Pre-Upload to YouTube beforehand as Private ──────────────
                history = UploadHistory(
                    channel_id=channel.id,
                    status="uploading",
                    video_title=video.title,
                    scheduled_at=sched.next_upload_at,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
                if thumbnail:
                    history.thumbnail_id = thumbnail.id
                db.add(history)
                await db.flush()

                def on_progress(bytes_uploaded, total_bytes):
                    upload_tracker.update_bytes(video.id, bytes_uploaded, total_bytes)

                upload_tracker.update_stage(video.id, "uploading", "Connecting & streaming video to YouTube beforehand (Private)...", 15)

                import json as _json
                tags = _json.loads(video.tags) if video.tags else []
                yt_svc = build_youtube_service_from_db(channel)

                t1 = time.monotonic()
                yt_video_id = await with_exponential_backoff(
                    _run_upload,
                    yt_svc,
                    output_path,
                    video.title,
                    video.description or "",
                    tags,
                    video.category_id or "22",
                    "private",
                    progress_callback=on_progress,
                    max_retries=3,
                    base_delay=5.0,
                    retriable_exceptions=(Exception,),
                )
                history.upload_duration = time.monotonic() - t1

                # ── Thumbnail Upload ────────────────────────────────────────
                if thumbnail and thumbnail.stored_path:
                    upload_tracker.update_stage(video.id, "finalizing", "Setting video thumbnail on YouTube (1s photo)...", 95)
                    try:
                        yt_svc.set_thumbnail(yt_video_id, thumbnail.stored_path)
                        log.info("Thumbnail successfully attached for preflight upload", yt_video_id=yt_video_id)
                    except Exception as exc:
                        log.warning("Thumbnail set failed for preflight upload", error=str(exc))

                # Update tokens
                token_data = yt_svc.get_encrypted_tokens()
                channel.encrypted_access_token = token_data["encrypted_access_token"]
                channel.encrypted_refresh_token = token_data["encrypted_refresh_token"]
                channel.token_expiry = token_data["token_expiry"]

                yt_url = f"https://www.youtube.com/watch?v={yt_video_id}"
                history.youtube_video_id = yt_video_id
                history.youtube_url = yt_url
                history.status = "waiting_release"
                await db.commit()

                # Schedule release at sched.next_upload_at
                from scheduler.engine import get_scheduler
                sch = get_scheduler()
                target_release_dt = None
                if sched.next_upload_at:
                    try:
                        iso_str = sched.next_upload_at.replace("Z", "+00:00")
                        target_release_dt = datetime.fromisoformat(iso_str)
                    except Exception:
                        target_release_dt = None

                if not target_release_dt or target_release_dt <= datetime.now(timezone.utc):
                    target_release_dt = datetime.now(timezone.utc) + timedelta(minutes=getattr(sched, 'pre_process_minutes', 30))

                release_job_id = f"release_scheduled_{channel.id}_{video.id}"
                privacy_on_release = getattr(video, "privacy_status", "public") or "public"
                sch.add_job(
                    release_video_and_announce_job,
                    trigger="date",
                    run_date=target_release_dt,
                    id=release_job_id,
                    args=[
                        channel.id,
                        video.id,
                        yt_video_id,
                        history.id,
                        privacy_on_release,
                        True,
                        thumbnail.stored_path if thumbnail else None,
                    ],
                    replace_existing=True,
                    misfire_grace_time=3600,
                )

                upload_tracker.update_stage(
                    video.id,
                    "waiting_release",
                    f"Uploaded beforehand! Auto-releasing at {sched.next_upload_at}",
                    95,
                )
                meta = {
                    "ok": True,
                    "status": "waiting_release",
                    "video_id": video.id,
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                    "history_id": history.id,
                    "youtube_video_id": yt_video_id,
                    "youtube_url": yt_url,
                    "release_at": sched.next_upload_at,
                    "privacy": privacy_on_release,
                    "send_discord": True,
                    "thumbnail_display_url": history.thumbnail_display_url,
                    "video_title": video.title,
                    "overlay_username": cur_user,
                    "overlay_password": cur_pwd,
                    "overlay_burn_time": f"{int(u_start)}s - {int(u_end)}s",
                }
                upload_tracker.update_metadata(video.id, meta)
                log.info("Pre-flight video processing & pre-upload complete! Waiting for release.", channel_id=channel_id, yt_video_id=yt_video_id)
            except UploadLimitExceededError as exc:
                clean_msg = (
                    "YouTube Daily Upload Limit Exceeded: Channel reached Google's 24h upload limit. "
                    "Enable Advanced Features in YouTube Studio or switch channels."
                )
                upload_tracker.fail(video.id, clean_msg)
                log.error("Pre-flight upload limit exceeded", channel_id=channel_id)
            except QuotaExceededError as exc:
                clean_msg = "YouTube API Quota Exceeded (10,000 units/day project cap)."
                upload_tracker.fail(video.id, clean_msg)
                log.error("Pre-flight quota exceeded", channel_id=channel_id)
            except Exception as exc:
                err_str = str(exc)
                if "uploadLimitExceeded" in err_str or "exceeded the number of videos" in err_str:
                    clean_msg = (
                        "YouTube Daily Upload Limit Exceeded: Channel reached Google's 24h upload limit. "
                        "Enable Advanced Features in YouTube Studio or switch channels."
                    )
                    upload_tracker.fail(video.id, clean_msg)
                else:
                    upload_tracker.fail(video.id, str(exc))
                log.exception("Pre-flight processing failed", channel_id=channel_id, error=str(exc))
    finally:
        _active_preflights.discard(channel_id)


# ── Credential rotation job ──────────────────────────────────────────────────

async def credential_rotation_job() -> None:
    """Rotate dashboard credentials and notify admin."""
    log.info("Running credential rotation job")
    async with get_db_context() as db:
        await rotate_credentials(db)


# ── Test upload function ─────────────────────────────────────────────────────

# ── Test upload function ─────────────────────────────────────────────────────

async def release_video_and_announce_job(
    channel_id: int,
    video_id: int,
    youtube_video_id: str,
    history_id: int,
    privacy: str = "public",
    send_discord: bool = True,
    thumbnail_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Releases a pre-uploaded YouTube video (changes privacy from private to public)
    and sends the announcement message to Discord.
    Guaranteed deduplication: Prevents double-firing between date job and daily scheduler.
    """
    log.info(
        "Executing video release and Discord announcement",
        channel_id=channel_id,
        video_id=video_id,
        yt_video_id=youtube_video_id,
        privacy=privacy,
        send_discord=send_discord,
    )

    # ── Cancel any existing scheduled release jobs to prevent duplicate runs ──
    try:
        from scheduler.engine import get_scheduler
        sch = get_scheduler()
        for jid in [f"release_scheduled_{channel_id}_{video_id}", f"release_test_{channel_id}_{video_id}"]:
            if sch.get_job(jid):
                sch.remove_job(jid)
                log.info("Cancelled pending scheduled release job to prevent duplicate execution", job_id=jid)
    except Exception as exc:
        log.debug("Scheduler job cleanup notice", error=str(exc))

    from services.upload_progress import upload_tracker
    upload_tracker.update_stage(
        video_id,
        "releasing",
        "Releasing video to YouTube & sending Discord announcement...",
        98,
    )

    async with get_db_context() as db:
        channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, channel_id)
        video: Optional[Video] = await db.get(Video, video_id)
        history: Optional[UploadHistory] = await db.get(UploadHistory, history_id)

        if not channel or not video:
            raise ValueError(f"Channel ({channel_id}) or Video ({video_id}) not found for release")

        if history:
            if history.status in ("success", "completed", "releasing") and history.discord_message_id:
                log.info("Video already released and announced, aborting duplicate", channel_id=channel_id, yt_video_id=youtube_video_id)
                yt_url = history.youtube_url or f"https://www.youtube.com/watch?v={youtube_video_id}"
                return {
                    "ok": True,
                    "status": "released",
                    "channel_id": channel_id,
                    "channel_name": channel.name,
                    "video_id": video.id,
                    "video_title": video.title,
                    "youtube_video_id": youtube_video_id,
                    "youtube_url": yt_url,
                    "privacy": privacy,
                    "discord_announced": bool(history.discord_message_id),
                    "discord_message_id": history.discord_message_id,
                }
            # Set to releasing immediately to lock against concurrent runners
            history.status = "releasing"
            await db.commit()

        yt_svc = build_youtube_service_from_db(channel)
        try:
            yt_svc.set_privacy(youtube_video_id, privacy)
            log.info("Video released on YouTube", video_id=youtube_video_id, privacy=privacy)
        except Exception as exc:
            log.warning("Could not set privacy on release", error=str(exc))

        discord_msg_id = None
        if send_discord:
            yt_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
            thumb_path = thumbnail_path
            if not thumb_path and history and history.thumbnail_id:
                thumb_rec = await db.get(Thumbnail, history.thumbnail_id)
                if thumb_rec:
                    thumb_path = thumb_rec.stored_path

            discord_msg_id = await send_upload_announcement(
                channel_id=channel_id,
                video_title=video.title,
                youtube_url=yt_url,
                thumbnail_path=thumb_path,
                channel_name=channel.name,
            )

        if history:
            history.status = "success"
            history.youtube_video_id = youtube_video_id
            history.youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
            if discord_msg_id:
                history.discord_message_id = str(discord_msg_id)
            history.completed_at = datetime.now(timezone.utc).isoformat()
            await db.commit()

        # ── Auto-comment trigger (for public AND unlisted releases) ────
        if (privacy or "").strip().lower() in ("public", "unlisted"):
            try:
                from services.comment_poster import trigger_auto_comment
                await trigger_auto_comment(
                    channel_id=channel_id,
                    video_id=video.id,
                    youtube_video_id=youtube_video_id,
                    video_title=video.title,
                )
            except Exception as exc:
                log.warning("Could not trigger auto-comment on release", error=str(exc))

        # ── Advance schedule to next upload cycle & rotate thumbnail ─────
        sched_res = await db.execute(
            select(ScheduleConfig).where(ScheduleConfig.channel_id == channel_id)
        )
        sched: Optional[ScheduleConfig] = sched_res.scalar_one_or_none()
        if sched and sched.is_active:
            try:
                from scheduler.timing import compute_next_upload_time, compute_next_process_time
                next_time = compute_next_upload_time(
                    sched.window_start_hour,
                    sched.window_end_hour,
                    sched.timezone,
                    schedule_mode=getattr(sched, 'schedule_mode', 'exact'),
                    upload_hour=getattr(sched, 'upload_hour', 14),
                    upload_minute=getattr(sched, 'upload_minute', 0),
                )
                sched.next_upload_at = next_time.isoformat()
                pre_proc_min = getattr(sched, 'pre_process_minutes', 30)
                process_time = compute_next_process_time(next_time, pre_proc_min)
                sched.next_process_at = process_time.isoformat()
                # Rotate thumbnail index for next scheduled upload
                if getattr(sched, 'thumbnail_mode', 'sequential') == 'sequential':
                    sched.thumbnail_index = (getattr(sched, 'thumbnail_index', 0) or 0) + 1
                _reschedule_channel(channel_id, next_time, pre_proc_min)
                await db.commit()
                log.info("Advanced schedule to next cycle after release", next_upload_at=sched.next_upload_at)
            except Exception as sched_adv_err:
                log.warning("Could not advance schedule after release", error=str(sched_adv_err))

        yt_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        result_data = {
            "ok": True,
            "status": "released",
            "channel_id": channel_id,
            "channel_name": channel.name,
            "video_id": video.id,
            "video_title": video.title,
            "youtube_video_id": youtube_video_id,
            "youtube_url": yt_url,
            "privacy": privacy,
            "discord_announced": discord_msg_id is not None,
            "discord_message_id": discord_msg_id,
            "next_upload_at": sched.next_upload_at if sched else None,
            "next_process_at": sched.next_process_at if sched else None,
        }
        upload_tracker.finish(video.id, result_data)

        # Clear old process tracker after short delay so UI resets cleanly for next schedule
        try:
            loop = asyncio.get_event_loop()
            loop.call_later(8.0, lambda: upload_tracker.clear(video.id))
        except Exception:
            pass

        return result_data


async def test_upload_video(
    video_id: int,
    privacy: str = "public",
    lead_minutes: int = 5,
    send_discord: bool = True,
) -> dict[str, Any]:
    """
    Directly test the scheduled upload pipeline:
      - Renders FFmpeg overlays (dynamic username & password codes)
      - Attaches the selected/rotating thumbnail
      - Uploads to YouTube beforehand as 'private' (or directly if lead_minutes == 0)
      - If lead_minutes > 0: schedules release at (now + lead_minutes),
        at which time privacy changes to 'public' and Discord announcement is sent!
      - If lead_minutes == 0: releases immediately and sends Discord announcement.
    """
    log.info(
        "Starting test video upload",
        video_id=video_id,
        privacy=privacy,
        lead_minutes=lead_minutes,
        send_discord=send_discord,
    )
    start = time.monotonic()

    async with get_db_context() as db:
        video: Optional[Video] = await db.get(Video, video_id)
        if not video:
            raise ValueError(f"Video ID {video_id} not found")

        channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, video.channel_id)
        if not channel:
            raise ValueError(f"Channel for video {video_id} not found")
        if not channel.is_authenticated:
            raise ValueError(f"Channel '{channel.name}' is not authenticated with YouTube yet. Please connect OAuth first.")

        # History record for test upload
        history = UploadHistory(
            channel_id=channel.id,
            status="processing",
            video_title=f"[TEST] {video.title}",
            scheduled_at=datetime.now(timezone.utc).isoformat(),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(history)
        await db.flush()

        from services.upload_progress import upload_tracker
        upload_tracker.start(video.id, total_bytes=video.file_size_bytes or 0, channel_id=channel.id)

        try:
            # ── 1. Thumbnail Selection & Standardization ────────────────
            upload_tracker.update_stage(video.id, "thumbnail", "Selecting & preparing thumbnail...", 4)
            sched_result = await db.execute(
                select(ScheduleConfig).where(ScheduleConfig.channel_id == channel.id)
            )
            sched: Optional[ScheduleConfig] = sched_result.scalar_one_or_none()

            thumbnail = await prepare_scheduled_thumbnail(db, channel.id, sched, video)
            if thumbnail:
                history.thumbnail_id = thumbnail.id
                upload_tracker.update_metadata(video.id, {
                    "thumbnail_id": thumbnail.id,
                    "thumbnail_display_url": thumbnail.web_url,
                    "thumbnail_filename": thumbnail.filename,
                })
                upload_tracker.update_stage(video.id, "thumbnail", f"Prepared thumbnail: {thumbnail.filename} (1280×720)", 6)

            # ── 2. FFmpeg Rendering with Overlays & Thumbnail Embedding ─
            output_path = str(PROCESSED_DIR / f"test_ch{channel.id}_{history.id}.mp4")
            t0 = time.monotonic()
            thumb_path = thumbnail.stored_path if (thumbnail and thumbnail.stored_path and os.path.exists(thumbnail.stored_path)) else None
            should_process = video.apply_ffmpeg or (thumb_path is not None)

            if should_process:
                overlays = []
                if video.apply_ffmpeg:
                    upload_tracker.update_stage(video.id, "ffmpeg", "Applying FFmpeg Text Overlays & Burns...", 8)
                    if getattr(video, "overlay_randomize_username", True):
                        cur_user = generate_random_code()
                    else:
                        admin_res = await db.execute(select(User).where(User.id == 1))
                        admin_user = admin_res.scalar_one_or_none()
                        cur_user = admin_user.username if admin_user else settings.admin_username

                    if getattr(video, "overlay_randomize_password", True):
                        cur_pwd = generate_random_code(exclude=cur_user)
                    else:
                        cur_pwd = settings.admin_password[:4] if len(settings.admin_password) >= 4 else (settings.admin_password + "99")[:4]

                    # Auto-create user on LicenseAuth / KeyAuth if enabled
                    try:
                        from services.licenseauth import auto_create_user_for_video
                        la_res = await auto_create_user_for_video(cur_user, cur_pwd, db=db)
                        if la_res.get("success"):
                            log.info("LicenseAuth test upload user auto-created successfully", user=cur_user)
                        elif not la_res.get("skipped"):
                            log.warning("LicenseAuth test upload user auto-creation returned error", user=cur_user, result=la_res)
                    except Exception as _la_err:
                        log.warning("LicenseAuth test upload user auto-creation error", error=str(_la_err))

                    u_start = video.overlay_username_start or 25.0
                    u_end = video.overlay_username_end or 35.0
                    p_start = video.overlay_password_start or 25.0
                    p_end = video.overlay_password_end or 35.0

                    if video.overlay_randomize_time:
                        min_t = video.overlay_random_p_min if video.overlay_random_p_min is not None else 25.0
                        max_t = video.overlay_random_p_max if video.overlay_random_p_max is not None else 60.0
                        if max_t > min_t:
                            dur = max(2.0, (video.overlay_password_end or 35.0) - (video.overlay_password_start or 25.0))
                            upper = max(min_t, max_t - dur)
                            shared_start = round(uniform(min_t, upper), 1)
                            shared_end = round(shared_start + dur, 1)
                            u_start = shared_start
                            u_end = shared_end
                            p_start = shared_start
                            p_end = shared_end

                    if video.overlay_username_enabled and video.overlay_username_text:
                        u_txt = video.overlay_username_text.replace("{username}", cur_user)
                        overlays.append({
                            "enabled": True,
                            "text": u_txt,
                            "start": u_start,
                            "end": u_end,
                            "position": video.overlay_username_position or "top-left",
                            "fontsize": video.overlay_username_font_size or 36,
                            "color": video.overlay_username_color or "#ffffff",
                        })

                    if video.overlay_password_enabled and video.overlay_password_text:
                        p_txt = video.overlay_password_text.replace("{password}", cur_pwd)
                        overlays.append({
                            "enabled": True,
                            "text": p_txt,
                            "start": p_start,
                            "end": p_end,
                            "position": video.overlay_password_position or "bottom-right",
                            "fontsize": video.overlay_password_font_size or 36,
                            "color": video.overlay_password_color or "#22c55e",
                        })

                    upload_tracker.update_stage(
                        video.id,
                        "ffmpeg",
                        f"Editing video: Burning User [{cur_user}] & Pass [{cur_pwd}] at {int(u_start)}s-{int(u_end)}s...",
                        10,
                    )
                    upload_tracker.update_metadata(video.id, {
                        "overlay_username": cur_user,
                        "overlay_password": cur_pwd,
                        "overlay_burn_time": f"{int(u_start)}s - {int(u_end)}s",
                        "edit_details": f"User: {cur_user} • Pass: {cur_pwd}",
                    })
                else:
                    upload_tracker.update_stage(video.id, "ffmpeg", "Embedding thumbnail into video intro...", 10)

                await process_video(
                    input_path=video.stored_path,
                    output_path=output_path,
                    trim_start=video.trim_start if video.apply_ffmpeg else None,
                    trim_end=video.trim_end if video.apply_ffmpeg else None,
                    intro_path=video.intro_path if video.apply_ffmpeg else None,
                    outro_path=video.outro_path if video.apply_ffmpeg else None,
                    overlays=overlays if video.apply_ffmpeg else None,
                    thumbnail_path=thumb_path,
                    thumbnail_intro_duration=1.0,
                )
            else:
                output_path = video.stored_path
                upload_tracker.update_stage(video.id, "ffmpeg", "Using original video (overlays off)...", 10)

            history.processing_duration = time.monotonic() - t0

            # ── 3. YouTube Upload ────────────────────────────────────────
            import json as _json
            tags = _json.loads(video.tags) if video.tags else []

            yt_svc = build_youtube_service_from_db(channel)
            history.status = "uploading"
            await db.commit()

            def on_progress(bytes_uploaded, total_bytes):
                upload_tracker.update_bytes(video.id, bytes_uploaded, total_bytes)

            upload_tracker.update_stage(video.id, "uploading", "Connecting & streaming video to YouTube...", 15)

            t1 = time.monotonic()
            # If lead_minutes > 0, upload beforehand as private!
            upload_privacy = "private" if lead_minutes > 0 else (privacy or "public")
            
            yt_video_id = await with_exponential_backoff(
                _run_upload,
                yt_svc,
                output_path,
                video.title,
                video.description or "",
                tags,
                video.category_id or "22",
                upload_privacy,
                progress_callback=on_progress,
                max_retries=3,
                base_delay=5.0,
                retriable_exceptions=(Exception,),
            )
            history.upload_duration = time.monotonic() - t1

            # ── 4. Thumbnail Upload ──────────────────────────────────────
            if thumbnail and thumbnail.stored_path:
                upload_tracker.update_stage(video.id, "finalizing", "Setting video thumbnail on YouTube (1s photo)...", 95)
                try:
                    yt_svc.set_thumbnail(yt_video_id, thumbnail.stored_path)
                    log.info("Thumbnail successfully attached for test upload", yt_video_id=yt_video_id)
                except Exception as exc:
                    log.warning("Thumbnail set failed for test upload", error=str(exc))
                    history.error_message = (history.error_message or "") + f" [Thumbnail warning: {exc}]"

            # Update tokens after refresh
            token_data = yt_svc.get_encrypted_tokens()
            channel.encrypted_access_token = token_data["encrypted_access_token"]
            channel.encrypted_refresh_token = token_data["encrypted_refresh_token"]
            channel.token_expiry = token_data["token_expiry"]

            yt_url = f"https://www.youtube.com/watch?v={yt_video_id}"
            history.youtube_video_id = yt_video_id
            history.youtube_url = yt_url

            # ── 5. Scheduled Delayed Release or Immediate ────────────────
            if lead_minutes > 0:
                release_time = datetime.now(timezone.utc) + timedelta(minutes=lead_minutes)
                history.status = "waiting_release"
                await db.commit()

                # Schedule the release and Discord announcement job
                from scheduler.engine import get_scheduler
                sch = get_scheduler()
                job_id = f"release_test_{channel.id}_{video.id}"
                sch.add_job(
                    release_video_and_announce_job,
                    trigger="date",
                    run_date=release_time,
                    id=job_id,
                    args=[
                        channel.id,
                        video.id,
                        yt_video_id,
                        history.id,
                        privacy,
                        send_discord,
                        thumbnail.stored_path if thumbnail else None,
                    ],
                    replace_existing=True,
                )

                upload_tracker.update_stage(
                    video.id,
                    "waiting_release",
                    f"Uploaded beforehand! Auto-releasing in {lead_minutes} min at {release_time.strftime('%I:%M:%S %p')} UTC",
                    95,
                )

                res_data = {
                    "ok": True,
                    "status": "waiting_release",
                    "video_id": video.id,
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                    "history_id": history.id,
                    "youtube_video_id": yt_video_id,
                    "youtube_url": yt_url,
                    "lead_minutes": lead_minutes,
                    "release_at": release_time.isoformat(),
                    "release_seconds": lead_minutes * 60,
                    "privacy": privacy,
                    "send_discord": send_discord,
                    "duration_seconds": round(time.monotonic() - start, 1),
                    "overlay_username": cur_user,
                    "overlay_password": cur_pwd,
                    "overlay_burn_time": f"{int(u_start)}s - {int(u_end)}s",
                    "thumbnail_display_url": history.thumbnail_display_url,
                    "video_title": video.title,
                }
                upload_tracker.update_metadata(video.id, res_data)
                return res_data

            else:
                # Immediate release
                discord_msg_id = None
                if send_discord:
                    discord_msg_id = await send_upload_announcement(
                        channel_id=channel.id,
                        video_title=video.title,
                        youtube_url=yt_url,
                        thumbnail_path=thumbnail.stored_path if thumbnail else None,
                        channel_name=channel.name,
                    )

                history.status = "success"
                if discord_msg_id:
                    history.discord_message_id = str(discord_msg_id)
                history.completed_at = datetime.now(timezone.utc).isoformat()
                await db.commit()

                # ── Auto-comment trigger (for public AND unlisted uploads) ────
                if (upload_privacy or "").strip().lower() in ("public", "unlisted"):
                    try:
                        from services.comment_poster import trigger_auto_comment
                        await trigger_auto_comment(
                            channel_id=channel.id,
                            video_id=video.id,
                            youtube_video_id=yt_video_id,
                            video_title=video.title,
                        )
                    except Exception as exc:
                        log.warning("Could not trigger auto-comment on test upload", error=str(exc))

                res_data = {
                    "ok": True,
                    "status": "released",
                    "video_id": video.id,
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                    "history_id": history.id,
                    "youtube_video_id": yt_video_id,
                    "youtube_url": yt_url,
                    "privacy": upload_privacy,
                    "discord_announced": discord_msg_id is not None,
                    "discord_message_id": discord_msg_id,
                    "duration_seconds": round(time.monotonic() - start, 1),
                }
                upload_tracker.finish(video.id, res_data)
                return res_data

        except UploadLimitExceededError as exc:
            clean_msg = (
                "YouTube Daily Upload Limit Exceeded: This channel reached Google's 24-hour upload limit. "
                "Enable Advanced Features in YouTube Studio or switch channels."
            )
            upload_tracker.fail(video.id, clean_msg)
            try:
                existing_h = await db.get(UploadHistory, history.id)
                if existing_h:
                    existing_h.status = "failed"
                    existing_h.error_message = clean_msg
                    existing_h.completed_at = datetime.now(timezone.utc).isoformat()
                    await db.commit()
            except Exception:
                await db.rollback()
            log.warning("Test upload hit YouTube daily upload limit", video_id=video_id)
            raise
        except QuotaExceededError as exc:
            clean_msg = "YouTube API Quota Exceeded (10,000 units/day project cap)."
            upload_tracker.fail(video.id, clean_msg)
            try:
                existing_h = await db.get(UploadHistory, history.id)
                if existing_h:
                    existing_h.status = "failed"
                    existing_h.error_message = clean_msg
                    existing_h.completed_at = datetime.now(timezone.utc).isoformat()
                    await db.commit()
            except Exception:
                await db.rollback()
            log.warning("Test upload hit YouTube API quota cap", video_id=video_id)
            raise
        except Exception as exc:
            err_str = str(exc)
            if "uploadLimitExceeded" in err_str or "exceeded the number of videos" in err_str:
                clean_msg = (
                    "YouTube Daily Upload Limit Exceeded: This channel reached Google's 24-hour upload limit. "
                    "Enable Advanced Features in YouTube Studio or switch channels."
                )
                upload_tracker.fail(video.id, clean_msg)
                try:
                    existing_h = await db.get(UploadHistory, history.id)
                    if existing_h:
                        existing_h.status = "failed"
                        existing_h.error_message = clean_msg
                        existing_h.completed_at = datetime.now(timezone.utc).isoformat()
                        await db.commit()
                except Exception:
                    await db.rollback()
                raise UploadLimitExceededError(clean_msg) from exc
            else:
                upload_tracker.fail(video.id, str(exc))
                try:
                    existing_h = await db.get(UploadHistory, history.id)
                    if existing_h:
                        existing_h.status = "failed"
                        existing_h.error_message = str(exc)[:1000]
                        existing_h.completed_at = datetime.now(timezone.utc).isoformat()
                        await db.commit()
                except Exception:
                    await db.rollback()
                log.exception("Test upload failed", video_id=video_id, error=str(exc))
                raise


async def test_schedule_upload(
    channel_id: int,
    privacy: str = "public",
    lead_minutes: int = 5,
    send_discord: bool = True,
) -> dict[str, Any]:
    """
    Test the upload schedule for a specific channel:
      1. Validates channel and YouTube authentication
      2. Finds the scheduled active video for this channel
      3. Executes the full pipeline with dynamic overlays and thumbnail rotation
      4. Uploads to YouTube beforehand as private
      5. Releases video and sends Discord announcement after lead_minutes (default 5 min)!
    """
    log.info(
        "Testing schedule upload",
        channel_id=channel_id,
        privacy=privacy,
        lead_minutes=lead_minutes,
        send_discord=send_discord,
    )
    async with get_db_context() as db:
        channel: Optional[YouTubeChannel] = await db.get(YouTubeChannel, channel_id)
        if not channel:
            raise ValueError(f"Channel ID {channel_id} not found")
        if not channel.is_authenticated:
            raise ValueError(f"Channel '{channel.name}' is not authenticated with YouTube yet. Please connect OAuth first.")

        vid_res = await db.execute(
            select(Video).where(Video.channel_id == channel_id, Video.is_active == True)
        )
        video = vid_res.scalar_one_or_none()
        if not video:
            raise ValueError("No active video is configured for this schedule. Please upload or select a video first.")

        video_id = video.id

    return await test_upload_video(
        video_id=video_id,
        privacy=privacy,
        lead_minutes=lead_minutes,
        send_discord=send_discord,
    )


