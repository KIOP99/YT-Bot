"""
run_bot.py
----------
Entrypoint that runs the FastAPI dashboard + all configured Discord bots
on the same asyncio event loop.

Multi-bot: bots are read from the database (discord_bots table).
The legacy single DISCORD_BOT_TOKEN in .env is still supported as a fallback
but per-channel DB tokens take priority.
"""

import asyncio
import uvicorn

from core.config import settings
from core.logging_config import get_logger, setup_logging

log = get_logger(__name__)


async def _launch_db_bots() -> None:
    """Start all enabled per-channel bots stored in the database."""
    try:
        from bot.manager import bot_manager
        from core.database import get_db_context
        from models.discord_bot import DiscordBot
        from sqlalchemy import select

        async with get_db_context() as db:
            res = await db.execute(
                select(DiscordBot).where(DiscordBot.is_enabled == True)
            )
            bots = res.scalars().all()

        for db_bot in bots:
            if db_bot.bot_token and db_bot.bot_token.strip():
                await bot_manager.start_bot(
                    channel_id=db_bot.channel_id,
                    token=db_bot.bot_token.strip(),
                    bot_name=db_bot.bot_name or "YTBot",
                )
                log.info("Started per-channel bot", channel_id=db_bot.channel_id)

    except Exception as exc:
        log.warning("Could not launch DB bots (table may not exist yet)", error=str(exc))


async def run_all():
    setup_logging()

    # Import after logging is set up
    from api.main import app

    config = uvicorn.Config(
        app=app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        timeout_keep_alive=60,
    )
    server = uvicorn.Server(config)

    # Wait for the server to be ready then start DB bots
    async def server_with_bots():
        # Small delay to let FastAPI lifespan run create_all_tables first
        await asyncio.sleep(3)
        await _launch_db_bots()

    # Run server + DB bot startup concurrently
    await asyncio.gather(
        server.serve(),
        server_with_bots(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    asyncio.run(run_all())
