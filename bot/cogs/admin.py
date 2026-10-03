"""
bot/cogs/admin.py
-----------------
Admin cog — slash commands removed per user request.
This file is kept as a placeholder so the extension loads cleanly.
"""

from __future__ import annotations

from discord.ext import commands


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
