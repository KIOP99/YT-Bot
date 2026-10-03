"""
scheduler/engine.py
--------------------
APScheduler setup using AsyncIOScheduler.
Loads all active channel schedules from DB on startup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
from sqlalchemy import select

from core.config import settings
from core.database import get_db_context
from core.logging_config import get_logger
from models.channel import YouTubeChannel
from models.schedule_config import ScheduleConfig

log = get_logger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        raise RuntimeError("Scheduler has not been initialized. Call init_scheduler() first.")
    return _scheduler


def init_scheduler() -> AsyncIOScheduler:
    """Create and configure the AsyncIOScheduler singleton."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    jobstores = {"default": MemoryJobStore()}
    executors = {"default": AsyncIOExecutor()}
    job_defaults = {
        "coalesce": True,       # merge missed runs into one
        "max_instances": 1,     # only one instance per job at a time
        "misfire_grace_time": 3600,  # 1 hour grace for misfires
    }

    _scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone=settings.scheduler_timezone,
    )
    log.info("Scheduler initialized", timezone=settings.scheduler_timezone)
    return _scheduler


async def start_scheduler() -> None:
    """Start the scheduler and load all active channel jobs from DB."""
    sched = init_scheduler()
    sched.start()
    log.info("Scheduler started")
    await _load_channel_jobs()
    _add_credential_rotation_job()


async def _load_channel_jobs() -> None:
    """Load all active schedule configs and schedule their next upload jobs."""
    from scheduler.jobs import daily_upload_job, compute_next_upload_time, compute_next_process_time, preflight_process_job

    async with get_db_context() as db:
        result = await db.execute(
            select(ScheduleConfig, YouTubeChannel)
            .join(YouTubeChannel, ScheduleConfig.channel_id == YouTubeChannel.id)
            .where(
                ScheduleConfig.is_active == True,
                YouTubeChannel.is_active == True,
                YouTubeChannel.is_authenticated == True,
            )
        )
        rows = result.all()

    sched = get_scheduler()
    for sched_cfg, channel in rows:
        job_id = f"upload_{channel.id}"
        process_job_id = f"process_{channel.id}"

        # Determine upload run time
        if sched_cfg.next_upload_at:
            try:
                run_at = datetime.fromisoformat(sched_cfg.next_upload_at)
                if run_at <= datetime.now(timezone.utc):
                    run_at = compute_next_upload_time(
                        sched_cfg.window_start_hour,
                        sched_cfg.window_end_hour,
                        sched_cfg.timezone,
                        schedule_mode=getattr(sched_cfg, 'schedule_mode', 'exact'),
                        upload_hour=getattr(sched_cfg, 'upload_hour', 14),
                        upload_minute=getattr(sched_cfg, 'upload_minute', 0),
                    )
            except ValueError:
                run_at = compute_next_upload_time(
                    sched_cfg.window_start_hour,
                    sched_cfg.window_end_hour,
                    sched_cfg.timezone,
                    schedule_mode=getattr(sched_cfg, 'schedule_mode', 'exact'),
                    upload_hour=getattr(sched_cfg, 'upload_hour', 14),
                    upload_minute=getattr(sched_cfg, 'upload_minute', 0),
                )
        else:
            run_at = compute_next_upload_time(
                sched_cfg.window_start_hour,
                sched_cfg.window_end_hour,
                sched_cfg.timezone,
                schedule_mode=getattr(sched_cfg, 'schedule_mode', 'exact'),
                upload_hour=getattr(sched_cfg, 'upload_hour', 14),
                upload_minute=getattr(sched_cfg, 'upload_minute', 0),
            )

        sched.add_job(
            daily_upload_job,
            trigger="date",
            run_date=run_at,
            id=job_id,
            name=f"Upload: {channel.name}",
            args=[channel.id],
            replace_existing=True,
        )
        log.info("Scheduled upload job", channel=channel.name, run_at=run_at.isoformat())

        # Schedule pre-processing job
        pre_min = getattr(sched_cfg, 'pre_process_minutes', 30)
        if pre_min > 0:
            now_utc = datetime.now(timezone.utc)
            process_at = compute_next_process_time(run_at, pre_min)
            if process_at > now_utc:
                sched.add_job(
                    preflight_process_job,
                    trigger="date",
                    run_date=process_at,
                    id=process_job_id,
                    name=f"Pre-process: {channel.name}",
                    args=[channel.id],
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                log.info("Scheduled preflight job", channel=channel.name, process_at=process_at.isoformat())
            elif process_at <= now_utc < run_at:
                sched.add_job(
                    preflight_process_job,
                    trigger="date",
                    run_date=now_utc + timedelta(seconds=3),
                    id=process_job_id,
                    name=f"Pre-process (catch-up): {channel.name}",
                    args=[channel.id],
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                log.info("Scheduled catch-up preflight job immediately", channel=channel.name)



def _add_credential_rotation_job() -> None:
    """Add credential rotation recurring job."""
    from scheduler.jobs import credential_rotation_job

    sched = get_scheduler()
    interval_hours = settings.cred_rotation_interval_hours
    sched.add_job(
        credential_rotation_job,
        trigger="interval",
        hours=interval_hours,
        id="credential_rotation",
        name="Credential Rotation",
        replace_existing=True,
    )
    log.info("Credential rotation scheduled", every_hours=interval_hours)


def add_channel_job(channel_id: int, run_at: datetime) -> None:
    """Add or replace an upload job for a channel."""
    from scheduler.jobs import daily_upload_job

    sched = get_scheduler()
    job_id = f"upload_{channel_id}"
    sched.add_job(
        daily_upload_job,
        trigger="date",
        run_date=run_at,
        id=job_id,
        args=[channel_id],
        replace_existing=True,
    )
    log.info("Upload job added/updated", channel_id=channel_id, run_at=run_at.isoformat())


def pause_channel_job(channel_id: int) -> None:
    sched = get_scheduler()
    try:
        sched.pause_job(f"upload_{channel_id}")
        log.info("Upload job paused", channel_id=channel_id)
    except Exception as exc:
        log.warning("Could not pause job", error=str(exc))


def resume_channel_job(channel_id: int) -> None:
    sched = get_scheduler()
    try:
        sched.resume_job(f"upload_{channel_id}")
        log.info("Upload job resumed", channel_id=channel_id)
    except Exception as exc:
        log.warning("Could not resume job", error=str(exc))


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")
    _scheduler = None
