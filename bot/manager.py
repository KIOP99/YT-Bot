"""
bot/manager.py
--------------
BotManager — spawns, tracks, and tears down independent Discord bot instances.
One bot per YouTube channel, each running on its own asyncio task with its own token.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord.ext import commands

from core.logging_config import get_logger

log = get_logger(__name__)

_intents = discord.Intents.default()
_intents.message_content = True
_intents.guilds = True
_intents.members = True


class ChannelBot(commands.Bot):
    """A lightweight Discord bot tied to one YouTube channel."""

    def __init__(self, channel_id: int, bot_name: str = "YTBot"):
        super().__init__(
            command_prefix="!",
            intents=_intents,
            help_command=None,
        )
        self.yt_channel_id = channel_id
        self.display_name = bot_name

    async def setup_hook(self) -> None:
        # Load minimal cogs — announcements only
        try:
            await self.load_extension("bot.cogs.announcements")
        except Exception as exc:
            log.warning("Could not load announcements cog", error=str(exc))

    async def on_ready(self) -> None:
        log.info(
            "Bot ready",
            bot=str(self.user),
            yt_channel_id=self.yt_channel_id,
            guild_count=len(self.guilds),
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="📹 Uploading videos daily",
            )
        )


class BotManager:
    """
    Manages a registry of ChannelBot instances.
    Each bot is keyed by YouTube channel_id.
    """

    def __init__(self):
        # channel_id -> ChannelBot
        self._bots: dict[int, ChannelBot] = {}
        # channel_id -> asyncio.Task
        self._tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_bot(self, channel_id: int) -> Optional[ChannelBot]:
        """Return the running bot instance for a YouTube channel."""
        return self._bots.get(channel_id)

    def all_bots(self) -> dict[int, ChannelBot]:
        return dict(self._bots)

    async def start_bot(self, channel_id: int, token: str, bot_name: str = "YTBot") -> None:
        """Spawn a new bot for the given channel (no-op if already running)."""
        if channel_id in self._tasks and not self._tasks[channel_id].done():
            log.info("Bot already running, skipping", channel_id=channel_id)
            return

        bot = ChannelBot(channel_id=channel_id, bot_name=bot_name)
        self._bots[channel_id] = bot

        task = asyncio.create_task(
            self._run_bot(channel_id, bot, token),
            name=f"bot-ch{channel_id}",
        )
        self._tasks[channel_id] = task
        log.info("Bot task created", channel_id=channel_id, bot_name=bot_name)

    async def stop_bot(self, channel_id: int) -> None:
        """Gracefully stop the bot for a channel."""
        bot = self._bots.pop(channel_id, None)
        task = self._tasks.pop(channel_id, None)

        if bot and not bot.is_closed():
            try:
                await bot.close()
            except Exception as exc:
                log.warning("Error closing bot", channel_id=channel_id, error=str(exc))

        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        log.info("Bot stopped", channel_id=channel_id)

    async def stop_all(self) -> None:
        """Stop every running bot — called on application shutdown."""
        for channel_id in list(self._bots.keys()):
            await self.stop_bot(channel_id)

    async def restart_bot(self, channel_id: int, token: str, bot_name: str = "YTBot") -> None:
        """Stop then re-start a bot (used when the token is updated in the UI)."""
        await self.stop_bot(channel_id)
        await asyncio.sleep(1)
        await self.start_bot(channel_id, token, bot_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_bot(self, channel_id: int, bot: ChannelBot, token: str) -> None:
        try:
            await bot.start(token)
        except discord.LoginFailure:
            log.error("Invalid bot token — bot cannot log in", channel_id=channel_id)
            # Update DB status
            await self._mark_error(channel_id, "Invalid bot token (LoginFailure)")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("Bot crashed", channel_id=channel_id, error=str(exc))
            await self._mark_error(channel_id, str(exc)[:500])
        finally:
            self._bots.pop(channel_id, None)
            self._tasks.pop(channel_id, None)

    @staticmethod
    async def _mark_error(channel_id: int, error_msg: str) -> None:
        """Persist last_error and is_running=False in the DB."""
        try:
            from core.database import get_db_context
            from models.discord_bot import DiscordBot
            from sqlalchemy import select

            async with get_db_context() as db:
                res = await db.execute(
                    select(DiscordBot).where(DiscordBot.channel_id == channel_id)
                )
                rec = res.scalar_one_or_none()
                if rec:
                    rec.is_running = False
                    rec.last_error = error_msg
                    await db.commit()
        except Exception:
            pass


# ── Singleton ──────────────────────────────────────────────────────────────
bot_manager = BotManager()
