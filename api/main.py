"""
api/main.py
-----------
FastAPI application factory. Registers routers, middleware, lifespan events,
and static file serving. Shares the asyncio loop with the Discord bot.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from api.middleware import add_middleware, limiter
from api.routers import auth, channels, comments, dashboard, discord_settings, schedule, security, thumbnails, videos, licenseauth, bots
from core.config import settings
from core.database import create_all_tables
from core.logging_config import get_logger, setup_logging
from core.security import hash_password
from scheduler.engine import start_scheduler, stop_scheduler

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    setup_logging()
    log.info("YTBot Dashboard starting", env=settings.app_env)

    # Create tables (dev) / run migrations (prod use Alembic)
    await create_all_tables()

    # Clean up any legacy unwanted auto-generated thumbnails from stock
    await _cleanup_auto_thumbnails()

    # Seed admin user if not present
    await _seed_admin_user()

    # Start the scheduler
    await start_scheduler()

    # Start bots from DB
    await _start_db_bots()

    yield  # application runs here

    # Stop all bots on shutdown
    from bot.manager import bot_manager
    await bot_manager.stop_all()

    await stop_scheduler()
    log.info("YTBot Dashboard stopped")


async def _start_db_bots() -> None:
    """Auto-start all enabled per-channel Discord bots from DB."""
    try:
        from bot.manager import bot_manager
        from core.database import AsyncSessionLocal
        from models.discord_bot import DiscordBot
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(DiscordBot).where(DiscordBot.is_enabled == True)
            )
            db_bots = res.scalars().all()

        for db_bot in db_bots:
            if db_bot.bot_token and db_bot.bot_token.strip():
                await bot_manager.start_bot(
                    channel_id=db_bot.channel_id,
                    token=db_bot.bot_token.strip(),
                    bot_name=db_bot.bot_name or "YTBot",
                )
    except Exception as exc:
        log.warning("Could not auto-start bots (table may not exist yet)", error=str(exc))


async def _cleanup_auto_thumbnails() -> None:
    """Purge legacy auto_video_*.jpg thumbnails from DB and filesystem."""
    from pathlib import Path
    from core.database import AsyncSessionLocal
    from models.thumbnail import Thumbnail
    from sqlalchemy import select

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Thumbnail).where(Thumbnail.filename.like("auto_video_%")))
            auto_thumbs = result.scalars().all()
            for t in auto_thumbs:
                try:
                    Path(t.stored_path).unlink(missing_ok=True)
                except Exception:
                    pass
                await db.delete(t)
            if auto_thumbs:
                await db.commit()
                log.info("Cleaned up legacy auto-thumbnails from stock", count=len(auto_thumbs))

        # Also remove any stranded auto_video files on disk
        thumb_dir = Path("uploads/thumbnails")
        if thumb_dir.exists():
            for p in thumb_dir.glob("auto_video_*.jpg"):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as exc:
        log.warning("Could not complete auto-thumbnail cleanup", error=str(exc))


async def _seed_admin_user() -> None:
    """Create the initial admin user on first run."""
    from core.database import AsyncSessionLocal
    from models.user import User
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == 1))
        existing = result.scalar_one_or_none()
        if existing is None:
            user = User(
                id=1,
                username=settings.admin_username,
                hashed_password=hash_password(settings.admin_password),
            )
            db.add(user)
            await db.commit()
            log.info("Admin user seeded", username=settings.admin_username)



def create_app() -> FastAPI:
    app = FastAPI(
        title="YTBot Dashboard",
        description="YouTube upload automation with Discord bot",
        version="1.0.0",
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # Rate limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Middleware
    add_middleware(app)

    # Static files
    app.mount("/static", StaticFiles(directory="frontend/static"), name="static")
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

    # Routers
    app.include_router(dashboard.router)
    app.include_router(auth.router)
    app.include_router(channels.router, prefix="/api")
    app.include_router(videos.router, prefix="/api")
    app.include_router(thumbnails.router, prefix="/api")
    app.include_router(schedule.router, prefix="/api")
    app.include_router(discord_settings.router, prefix="/api")
    app.include_router(bots.router, prefix="/api")
    app.include_router(comments.router, prefix="/api")
    app.include_router(licenseauth.router, prefix="/api")
    app.include_router(security.router)

    # Friendly redirect: unauthenticated browser hits redirect to login
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        accept_header = request.headers.get("accept", "")
        if exc.status_code == 401 and "text/html" in accept_header and not request.url.path.startswith("/api/"):
            return RedirectResponse(url="/auth/login", status_code=302)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/login", include_in_schema=False)
    async def login_alias():
        return RedirectResponse(url="/auth/login", status_code=302)

    @app.get("/comments", include_in_schema=False)
    async def comments_alias():
        return RedirectResponse(url="/api/comments", status_code=302)

    @app.get("/license-auth", include_in_schema=False)
    async def licenseauth_alias():
        return RedirectResponse(url="/api/license-auth", status_code=302)

    return app


app = create_app()
