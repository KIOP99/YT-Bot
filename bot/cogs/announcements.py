"""bot/cogs/announcements.py — Listener cog (announcements sent programmatically)."""

from discord.ext import commands


class AnnouncementsCog(commands.Cog, name="Announcements"):
    """
    Announcements are sent by services/discord_notify.py directly.
    This cog exists as a placeholder for future event listeners.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AnnouncementsCog(bot))
