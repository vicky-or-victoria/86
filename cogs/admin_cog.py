import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import ensure_hexes, SAFE_HUB, STATUS_PLAYER


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return (interaction.user.guild_permissions.administrator or
                interaction.guild.owner_id == interaction.user.id)

    @app_commands.command(name="game_start", description="[Admin] Start the game for this server.")
    async def game_start(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await ensure_hexes(interaction.guild_id, conn)
            # Ensure Hex A is always player-controlled at start
            await conn.execute(
                "UPDATE hexes SET controller='players', status=$1 "
                "WHERE guild_id=$2 AND (address=$3 OR parent_address=$3 OR address LIKE $4)",
                STATUS_PLAYER, interaction.guild_id, SAFE_HUB, f"{SAFE_HUB}-%"
            )
            await conn.execute(
                "UPDATE guild_config SET game_started=TRUE WHERE guild_id=$1",
                interaction.guild_id,
            )

        embed = discord.Embed(
            title="🚨 The War Begins",
            description=(
                "The game has started. The Legion advances.\n\n"
                "**Hex A** is under player control — your safe hub.\n\n"
                "Handlers — register your squadrons with `/squadron_register`.\n"
                "Gamemasters — use `/set_gamemaster_role` to assign Legion control."
            ),
            color=discord.Color.red(),
        )
        embed.set_footer(text="86 — Eighty Six | All units, stand by.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="game_stop", description="[Admin] Pause the game.")
    async def game_stop(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_config SET game_started=FALSE WHERE guild_id=$1",
                interaction.guild_id,
            )
        await interaction.response.send_message("⏸️ Game paused.", ephemeral=True)

    @app_commands.command(name="game_reset", description="[Admin] Reset the game entirely for this server.")
    async def game_reset(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM squadrons WHERE guild_id=$1", interaction.guild_id)
            await conn.execute("DELETE FROM legion_units WHERE guild_id=$1", interaction.guild_id)
            await conn.execute("DELETE FROM combat_log WHERE guild_id=$1", interaction.guild_id)
            await conn.execute("DELETE FROM turn_history WHERE guild_id=$1", interaction.guild_id)
            await conn.execute("DELETE FROM legion_gm_moves WHERE guild_id=$1", interaction.guild_id)
            await conn.execute(
                "UPDATE hexes SET controller='neutral', status='neutral' WHERE guild_id=$1",
                interaction.guild_id
            )
            # Restore Hex A
            await conn.execute(
                "UPDATE hexes SET controller='players', status=$1 "
                "WHERE guild_id=$2 AND (address=$3 OR address LIKE $4)",
                STATUS_PLAYER, interaction.guild_id, SAFE_HUB, f"{SAFE_HUB}-%"
            )
            await conn.execute(
                "UPDATE guild_config SET game_started=FALSE, last_turn_at=NOW() WHERE guild_id=$1",
                interaction.guild_id
            )

        await interaction.response.send_message("🔄 Game reset. All data cleared. Hex A restored.", ephemeral=False)

    @app_commands.command(name="set_turn_interval", description="[Admin] Set hours between turns.")
    @app_commands.describe(hours="Turn interval in hours (1–168)")
    async def set_turn_interval(self, interaction: discord.Interaction, hours: int):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        if not (1 <= hours <= 168):
            await interaction.response.send_message("❌ Must be 1–168 hours.", ephemeral=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_config SET turn_interval_hours=$1 WHERE guild_id=$2",
                hours, interaction.guild_id,
            )
        await interaction.response.send_message(f"✅ Turn interval set to **{hours}h**.", ephemeral=True)

    @app_commands.command(name="game_status", description="View current war status.")
    async def game_status(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT * FROM guild_config WHERE guild_id=$1", interaction.guild_id)
            player_count = await conn.fetchval(
                "SELECT COUNT(DISTINCT owner_id) FROM squadrons WHERE guild_id=$1 AND is_active=TRUE",
                interaction.guild_id)
            turn_count = await conn.fetchval(
                "SELECT COUNT(*) FROM turn_history WHERE guild_id=$1", interaction.guild_id)
            hex_stats = await conn.fetch(
                "SELECT status, COUNT(*) as cnt FROM hexes WHERE guild_id=$1 AND level=1 GROUP BY status",
                interaction.guild_id)
            legion_count = await conn.fetchval(
                "SELECT COUNT(*) FROM legion_units WHERE guild_id=$1 AND is_active=TRUE",
                interaction.guild_id)
            gm_role_id = config["gamemaster_role_id"] if config else None

        hex_info = {r["status"]: r["cnt"] for r in hex_stats}
        gm_text = f"<@&{gm_role_id}>" if gm_role_id else "Not set — use `/set_gamemaster_role`"

        embed = discord.Embed(title="📊 War Status", color=discord.Color.blurple())
        embed.add_field(name="Status", value="🟢 Active" if config["game_started"] else "🔴 Paused", inline=True)
        embed.add_field(name="Turn Interval", value=f"{config['turn_interval_hours']}h", inline=True)
        embed.add_field(name="Turns Resolved", value=str(turn_count), inline=True)
        embed.add_field(name="Active Handlers", value=str(player_count), inline=True)
        embed.add_field(name="Active Legion Units", value=str(legion_count), inline=True)
        embed.add_field(name="Gamemaster Role", value=gm_text, inline=True)
        embed.add_field(
            name="Outer Hex Status",
            value="\n".join(f"**{k}**: {v}" for k, v in hex_info.items()) or "No data",
            inline=False,
        )
        embed.set_footer(text=f"Last turn: {config['last_turn_at'].strftime('%Y-%m-%d %H:%M UTC')}")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
