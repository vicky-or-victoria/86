"""
Legion control cog — for the bot owner and users with the Gamemaster role.
GMs can move Legion units manually before each turn resolves.
Unmoved units will be handled by the AI.
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import OUTER_LABELS, SAFE_HUB, outer_of


async def is_gm(interaction: discord.Interaction) -> bool:
    """Check if user is server owner or has the Gamemaster role."""
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


class LegionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="legion_list", description="[GM] List all active Legion units.")
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
            await interaction.response.send_message("No active Legion units.", ephemeral=True)
            return

        embed = discord.Embed(title="🔴 Active Legion Units", color=discord.Color.red())
        lines = []
        for u in units:
            moved = "🎮 GM moved" if u["manually_moved"] else "🤖 AI"
            lines.append(f"`ID {u['id']}` **{u['unit_type']}** @ `{u['hex_address']}` — {moved}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="legion_move", description="[GM] Order a Legion unit to move to a hex.")
    @app_commands.describe(
        unit_id="Legion unit ID (from /legion_list)",
        address="Target hex address",
    )
    async def legion_move(self, interaction: discord.Interaction, unit_id: int, address: str):
        if not await is_gm(interaction):
            await interaction.response.send_message("❌ Gamemaster only.", ephemeral=True)
            return

        address = address.strip().upper()

        # Legion can never enter Hex A
        if outer_of(address) == SAFE_HUB:
            await interaction.response.send_message(
                f"❌ Legion cannot enter Hex A or any of its sub-hexes.", ephemeral=True)
            return

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
                interaction.guild_id, address
            )
            if not hex_exists:
                await interaction.response.send_message(
                    f"❌ Hex `{address}` doesn't exist.", ephemeral=True)
                return

            # Queue the move (will be applied at turn resolution)
            await conn.execute(
                """INSERT INTO legion_gm_moves (guild_id, legion_unit_id, target_address)
                   VALUES ($1,$2,$3)
                   ON CONFLICT DO NOTHING""",
                interaction.guild_id, unit_id, address
            )

        await interaction.response.send_message(
            f"🎮 Queued: **Legion {unit['unit_type']}** (ID {unit_id}) → **{address}** next turn.",
            ephemeral=True
        )

    @app_commands.command(name="legion_spawn", description="[GM] Manually spawn a Legion unit at a hex.")
    @app_commands.describe(
        address="Hex address to spawn at (cannot be Hex A or its children)",
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
                "❌ Legion cannot spawn in Hex A.", ephemeral=True)
            return

        from utils.combat import legion_unit_for_hex
        unit = legion_unit_for_hex(address)

        pool = await get_pool()
        async with pool.acquire() as conn:
            hex_exists = await conn.fetchrow(
                "SELECT address FROM hexes WHERE guild_id=$1 AND address=$2",
                interaction.guild_id, address
            )
            if not hex_exists:
                await interaction.response.send_message(
                    f"❌ Hex `{address}` doesn't exist.", ephemeral=True)
                return

            await conn.execute(
                """INSERT INTO legion_units
                   (guild_id, unit_type, hex_address, attack, defense, speed, morale, supply, recon, manually_moved)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)""",
                interaction.guild_id, unit_type, address,
                unit.attack, unit.defense, unit.speed,
                unit.morale, unit.supply, unit.recon
            )

        await interaction.response.send_message(
            f"🔴 Spawned **Legion {unit_type}** at **{address}**.", ephemeral=False
        )

    @app_commands.command(name="set_gamemaster_role",
                          description="[Owner] Set the role that can control the Legion.")
    @app_commands.describe(role="The role to grant Gamemaster powers")
    async def set_gm_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild.owner_id != interaction.user.id:
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
            f"✅ **{role.name}** is now the Gamemaster role. Members with this role can control the Legion.",
            ephemeral=False
        )

    @app_commands.command(name="legion_pending", description="[GM] View queued Legion moves for this turn.")
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
            await interaction.response.send_message("No pending GM moves this turn.", ephemeral=True)
            return

        lines = [f"`ID {m['legion_unit_id']}` **{m['unit_type']}**: `{m['hex_address']}` → `{m['target_address']}`"
                 for m in moves]
        embed = discord.Embed(
            title="🎮 Pending Legion Moves",
            description="\n".join(lines),
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LegionCog(bot))
