"""
bot/client.py
--------------
Discord bot client setup with all intents configured.
The bot shares the asyncio event loop with the FastAPI application.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from core.config import settings
from core.logging_config import get_logger
from services.discord_notify import set_bot

log = get_logger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True


class YTBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",  # prefix not heavily used; slash commands are primary
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        """Called by discord.py before the bot connects."""
        # Load cogs
        await self.load_extension("bot.cogs.admin")
        await self.load_extension("bot.cogs.announcements")

        # Sync slash commands — skip gracefully if bot not in guild yet
        try:
            guild = discord.Object(id=settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild", guild_id=settings.discord_guild_id)
        except discord.errors.Forbidden:
            log.warning(
                "Could not sync slash commands — bot may not be in the guild yet. "
                "Invite the bot to your server and restart."
            )
        except Exception as exc:
            log.warning("Slash command sync skipped", error=str(exc))

    async def on_ready(self) -> None:
        set_bot(self)  # make bot available to services
        log.info(
            "Bot ready",
            user=str(self.user),
            guild_count=len(self.guilds),
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="📹 Uploading videos daily",
            )
        )

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        log.error("Command error", command=ctx.command, error=str(error))


# Singleton bot instance
bot = YTBot()
