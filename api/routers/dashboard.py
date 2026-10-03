"""
api/routers/dashboard.py
-------------------------
Overview dashboard: stats, countdown to next upload, quick status.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates import templates
from api.dependencies import require_admin
from core.database import get_db
from core.security import generate_csrf_token
from models.channel import YouTubeChannel
from models.schedule_config import ScheduleConfig
from models.upload_history import UploadHistory
from models.video import Video

from sqlalchemy.orm import selectinload

router = APIRouter(prefix="", tags=["Dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Active channels
    ch_result = await db.execute(
        select(YouTubeChannel).where(YouTubeChannel.is_active == True)
    )
    channels = ch_result.scalars().all()

    # Next uploads
    sched_result = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.is_active == True)
    )
    schedules = sched_result.scalars().all()

    # Active videos per channel
    vids_result = await db.execute(
        select(Video).where(Video.is_active == True)
    )
    active_videos_map = {v.channel_id: v for v in vids_result.scalars().all()}

    # Recent history
    hist_result = await db.execute(
        select(UploadHistory)
        .options(selectinload(UploadHistory.thumbnail))
        .order_by(UploadHistory.id.desc())
        .limit(15)
    )
    recent_uploads = hist_result.scalars().all()

    # Total successes
    success_result = await db.execute(
        select(func.count(UploadHistory.id)).where(UploadHistory.status == "success")
    )
    total_successes = success_result.scalar_one()

    csrf = generate_csrf_token()
    now = datetime.now(timezone.utc)

    resp = templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "channels": channels,
            "schedules": schedules,
            "active_videos_map": active_videos_map,
            "recent_uploads": recent_uploads,
            "total_successes": total_successes,
            "now_utc": now.isoformat(),
            "csrf_token": csrf,
        },
    )
    resp.set_cookie("csrf_token", csrf, httponly=False, samesite="strict")
    return resp


@router.get("/api/stats")
async def stats_api(
    user=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """JSON stats endpoint polled by the dashboard for live updates."""
    sched_result = await db.execute(select(ScheduleConfig))
    schedules = sched_result.scalars().all()

    success_result = await db.execute(
        select(func.count(UploadHistory.id)).where(UploadHistory.status == "success")
    )
    fail_result = await db.execute(
        select(func.count(UploadHistory.id)).where(UploadHistory.status == "failed")
    )

    return {
        "total_successes": success_result.scalar_one(),
        "total_failures": fail_result.scalar_one(),
        "schedules": [
            {
                "channel_id": s.channel_id,
                "is_active": s.is_active,
                "next_upload_at": s.next_upload_at,
            }
            for s in schedules
        ],
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
    }
