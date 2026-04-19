import asyncio
import logging
import os

import discord
from discord.ext import commands

from utils.db import init_schema, close_pool
from utils.turn_engine import TurnEngine

# The Discord user ID of the bot owner.
# This person can use all Admin AND GM commands in any server, regardless of their roles.
BOT_OWNER_ID: int = 0  # <-- Replace 0 with your Discord user ID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

COGS = [
    "cogs.map_cog",
    "cogs.squadron_cog",
    "cogs.admin_cog",
    "cogs.legion_cog",
]

intents = discord.Intents.default()
intents.message_content = False


class EightySixBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.turn_engine = None
        self.bot_owner_id: int = BOT_OWNER_ID

    async def setup_hook(self):
        await init_schema()
        log.info("Database schema initialized.")
        for cog in COGS:
            await self.load_extension(cog)
            log.info(f"Loaded cog: {cog}")
        await self.tree.sync()
        log.info("Slash commands synced.")
        self.turn_engine = TurnEngine(self)
        self.turn_engine.start()
        log.info("Turn engine started.")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name="Fighting at the Front Lines")
        )

    async def close(self):
        if self.turn_engine:
            self.turn_engine.stop()
        await close_pool()
        await super().close()


async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable not set.")
    bot = EightySixBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
