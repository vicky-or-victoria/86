"""
Legion control cog — for the bot owner and users with the Gamemaster role.
GMs can order Legion units manually before each turn resolves.
Unmoved units will be handled autonomously by the hivemind AI.
"""

import random
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import OUTER_LABELS, SAFE_HUB, SUB_POSITIONS, outer_of, level_of


async def is_gm(interaction: discord.Interaction) -> bool:
    """Check if user is the bot owner, server owner, or has the Gamemaster role."""
    # Bot owner (BOT_OWNER_ID env var) has GM access in every server
    bot_owner_id = getattr(interaction.client, "bot_owner_id", 0)
    if bot_owner_id and interaction.user.id == bot_owner_id:
        return True
    if interaction.guild.owner_id == interaction.user.id:
        return True
    pool = await get_pool()
    async with pool.acquire() as conn:
        config = await conn.fetchrow(
            "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1",
            interaction.guild_id
        )
    if config and config["gamemaster_role_id"]:
        role = interaction.guild.get_role(config["gamemaster_role_id"])
        if role and role in interaction.user.roles:
            return True
    return False


def _resolve_to_level3(address: str) -> str:
    lvl = level_of(address)
    if lvl == 1:
        mid = random.choice(SUB_POSITIONS)
        inner = random.choice(SUB_POSITIONS)
        return f"{address}-{mid}-{inner}"
    elif lvl == 2:
        inner = random.choice(SUB_POSITIONS)
        return f"{address}-{inner}"
    return address


class LegionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="legion_list", description="[GM] List all active Legion units on the front.")
    async def legion_list(self, interaction: discord.Interaction):
        if not await is_gm(interaction):
            await interaction.response.send_message("❌ Gamemaster only.", ephemeral=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            units = await conn.fetch(
                "SELECT id, unit_type, hex_address, manually_moved FROM legion_units "
                "WHERE guild_id=$1 AND is_active=TRUE ORDER BY hex_address",
                interaction.guild_id
            )
        if not units:
            await interaction.response.send_message("No active Legion units on the front.", ephemeral=True)
            return
        embed = discord.Embed(title="🔴 Active Legion Units — The Hivemind", color=discord.Color.red())
        lines = []
        for u in units:
            moved = "🎮 GM directed" if u["manually_moved"] else "🤖 Hivemind"
            lines.append(f"`ID {u['id']}` **{u['unit_type']}** @ `{u['hex_address']}` — {moved}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="legion_move", description="[GM] Direct a Legion unit to a sector.")
    @app_commands.describe(
        unit_id="Legion unit ID (from /legion_list)",
        address="Target sector address (level-3 preferred; coarser picks randomly within)",
    )
    async def legion_move(self, interaction: discord.Interaction, unit_id: int, address: str):
        if not await is_gm(interaction):
            await interaction.response.send_message("❌ Gamemaster only.", ephemeral=True)
            return
        address = address.strip().upper()
        if outer_of(address) == SAFE_HUB:
            await interaction.response.send_message(
                "❌ The Legion cannot breach the Citadel — Sector A is off limits.", ephemeral=True)
            return
        resolved = _resolve_to_level3(address)
        pool = await get_pool()
        async with pool.acquire() as conn:
            unit = await conn.fetchrow(
                "SELECT id, unit_type, hex_address FROM legion_units "
                "WHERE id=$1 AND guild_id=$2 AND is_active=TRUE",
                unit_id, interaction.guild_id
            )
            if not unit:
                await interaction.response.send_message(
                    f"❌ No active Legion unit with ID {unit_id}.", ephemeral=True)
                return
            hex_exists = await conn.fetchrow(
                "SELECT address FROM hexes WHERE guild_id=$1 AND address=$2",
                interaction.guild_id, resolved
            )
            if not hex_exists:
                await interaction.response.send_message(
                    f"❌ Sector `{resolved}` doesn't exist. Has the war been started with `/game_start`?", ephemeral=True)
                return
            await conn.execute(
                """INSERT INTO legion_gm_moves (guild_id, legion_unit_id, target_address)
                   VALUES ($1,$2,$3)
                   ON CONFLICT (guild_id, legion_unit_id) DO UPDATE SET target_address=$3""",
                interaction.guild_id, unit_id, resolved
            )
        note = f" (resolved from `{address}`)" if resolved != address else ""
        await interaction.response.send_message(
            f"🎮 Directed: **Legion {unit['unit_type']}** (ID {unit_id}) → `{resolved}`{note} next advance.",
            ephemeral=True
        )

    @app_commands.command(name="legion_spawn", description="[GM] Manually deploy a Legion unit to a sector.")
    @app_commands.describe(
        address="Sector address (e.g. 'G', 'G-3', or 'G-3-2'). Coarser addresses spawn randomly within.",
        unit_type="Unit type (Grauwolf, Löwe, Dinosauria, Juggernaut, Shepherd)",
    )
    async def legion_spawn(self, interaction: discord.Interaction, address: str,
                           unit_type: str = "Grauwolf"):
        if not await is_gm(interaction):
            await interaction.response.send_message("❌ Gamemaster only.", ephemeral=True)
            return
        address = address.strip().upper()
        if outer_of(address) == SAFE_HUB:
            await interaction.response.send_message(
                "❌ The Legion cannot breach the Citadel — Sector A is off limits.", ephemeral=True)
            return
        resolved = _resolve_to_level3(address)
        from utils.combat import legion_unit_for_hex
        unit = legion_unit_for_hex(resolved)
        pool = await get_pool()
        async with pool.acquire() as conn:
            hex_exists = await conn.fetchrow(
                "SELECT address FROM hexes WHERE guild_id=$1 AND address=$2",
                interaction.guild_id, resolved
            )
            if not hex_exists:
                await interaction.response.send_message(
                    f"❌ Sector `{resolved}` doesn't exist. Has the war been started with `/game_start`?",
                    ephemeral=True)
                return
            await conn.execute(
                """INSERT INTO legion_units
                   (guild_id, unit_type, hex_address, attack, defense, speed, morale, supply, recon, manually_moved)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)""",
                interaction.guild_id, unit_type, resolved,
                unit.attack, unit.defense, unit.speed,
                unit.morale, unit.supply, unit.recon
            )
        note = f" (randomly placed within `{address}`)" if resolved != address else ""
        await interaction.response.send_message(
            f"🔴 Deployed **Legion {unit_type}** at `{resolved}`{note}.", ephemeral=False
        )

    @app_commands.command(name="set_gamemaster_role",
                          description="[Owner] Assign the role that controls the Legion hivemind.")
    @app_commands.describe(role="The role to grant Gamemaster powers over the Legion")
    async def set_gm_role(self, interaction: discord.Interaction, role: discord.Role):
        bot_owner_id = getattr(interaction.client, "bot_owner_id", 0)
        is_bot_owner = bot_owner_id and interaction.user.id == bot_owner_id
        if not is_bot_owner and interaction.guild.owner_id != interaction.user.id:
            await interaction.response.send_message("❌ Server owner only.", ephemeral=True)
            return
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_config SET gamemaster_role_id=$1 WHERE guild_id=$2",
                role.id, interaction.guild_id
            )
        await interaction.response.send_message(
            f"✅ **{role.name}** is now the Gamemaster role — the hivemind answers to them.", ephemeral=False
        )

    @app_commands.command(name="legion_pending", description="[GM] View queued Legion directives for this advance.")
    async def legion_pending(self, interaction: discord.Interaction):
        if not await is_gm(interaction):
            await interaction.response.send_message("❌ Gamemaster only.", ephemeral=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            moves = await conn.fetch(
                """SELECT gm.legion_unit_id, gm.target_address, lu.unit_type, lu.hex_address
                   FROM legion_gm_moves gm
                   JOIN legion_units lu ON lu.id = gm.legion_unit_id
                   WHERE gm.guild_id=$1""",
                interaction.guild_id
            )
        if not moves:
            await interaction.response.send_message("No pending GM directives this advance.", ephemeral=True)
            return
        lines = [
            f"`ID {m['legion_unit_id']}` **{m['unit_type']}**: `{m['hex_address']}` → `{m['target_address']}`"
            for m in moves
        ]
        embed = discord.Embed(
            title="🎮 Pending Legion Directives",
            description="\n".join(lines),
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="force_turn", description="[GM] Immediately trigger the Legion's next advance.")
    async def force_turn(self, interaction: discord.Interaction):
        if not await is_gm(interaction):
            await interaction.response.send_message("❌ Gamemaster only.", ephemeral=True)
            return
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT game_started FROM guild_config WHERE guild_id=$1", interaction.guild_id
            )
        if not config or not config["game_started"]:
            await interaction.response.send_message(
                "❌ The war hasn't begun yet. Use `/game_start` first.", ephemeral=True)
            return

        await interaction.response.send_message(
            "⚡ Forcing Legion advance now...", ephemeral=True)

        try:
            async with pool.acquire() as conn:
                await self.bot.turn_engine._resolve_turn(conn, interaction.guild_id)
        except Exception as e:
            await interaction.followup.send(f"❌ Advance resolution failed: `{e}`", ephemeral=True)
            return

        await interaction.followup.send("✅ Legion advance resolved successfully.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(LegionCog(bot))
