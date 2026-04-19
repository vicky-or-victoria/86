import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import ensure_hexes, SAFE_HUB, STATUS_PLAYER


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        # Bot owner (BOT_OWNER_ID env var) always has admin access in any server
        if interaction.user.id == self.bot.bot_owner_id:
            return True
        return (interaction.user.guild_permissions.administrator or
                interaction.guild.owner_id == interaction.user.id)

    @app_commands.command(name="game_start", description="[Admin] Mobilise Squadron 86 — begin the war.")
    async def game_start(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Command staff only.", ephemeral=True)
            return

        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await ensure_hexes(interaction.guild_id, conn)
            hub_sub_addresses = [f"{SAFE_HUB}-{p}" for p in ["C","1","2","3","4","5","6"]]
            hub_inner_addresses = [
                f"{SAFE_HUB}-{p}-{q}"
                for p in ["C","1","2","3","4","5","6"]
                for q in ["C","1","2","3","4","5","6"]
            ]
            all_hub_addresses = [SAFE_HUB] + hub_sub_addresses + hub_inner_addresses
            await conn.execute(
                "UPDATE hexes SET controller='players', status=$1 "
                "WHERE guild_id=$2 AND address = ANY($3::text[])",
                STATUS_PLAYER, interaction.guild_id, all_hub_addresses
            )
            await conn.execute(
                "UPDATE guild_config SET game_started=TRUE WHERE guild_id=$1",
                interaction.guild_id,
            )

        embed = discord.Embed(
            title="🚨 The Legion Advances — War Begins",
            description=(
                "**It is 2086 AD.** The Legion's assault has reached critical mass.\n\n"
                "Squadron **86** has been mobilised. The front lines are active.\n\n"
                "**Sector A** — the capital citadel — is secured under Handler control.\n\n"
                "**Command Staff** — use `/post_registration` to open Handler enlistment.\n"
                "**Gamemasters** — use `/set_gamemaster_role` to assign Legion control.\n"
                "**Admins** — use `/set_handler_role` to assign a role to enlisted Handlers."
            ),
            color=discord.Color.red(),
        )
        embed.set_footer(text="Risk Universalis 3 — Squadron 86 | All Numbers, stand by.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="game_stop", description="[Admin] Pause the war.")
    async def game_stop(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Command staff only.", ephemeral=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_config SET game_started=FALSE WHERE guild_id=$1",
                interaction.guild_id,
            )
        await interaction.response.send_message("⏸️ War paused by Command.", ephemeral=True)

    @app_commands.command(name="game_reset", description="[Admin] Reset the war entirely — wipe all data.")
    async def game_reset(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Command staff only.", ephemeral=True)
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
            hub_sub_addresses = [f"{SAFE_HUB}-{p}" for p in ["C","1","2","3","4","5","6"]]
            hub_inner_addresses = [
                f"{SAFE_HUB}-{p}-{q}"
                for p in ["C","1","2","3","4","5","6"]
                for q in ["C","1","2","3","4","5","6"]
            ]
            all_hub_addresses = [SAFE_HUB] + hub_sub_addresses + hub_inner_addresses
            await conn.execute(
                "UPDATE hexes SET controller='players', status=$1 "
                "WHERE guild_id=$2 AND address = ANY($3::text[])",
                STATUS_PLAYER, interaction.guild_id, all_hub_addresses
            )
            await conn.execute(
                "UPDATE guild_config SET game_started=FALSE, last_turn_at=NOW() WHERE guild_id=$1",
                interaction.guild_id
            )

        # Strip the handler role from all players on reset
        try:
            async with pool.acquire() as conn2:
                cfg = await conn2.fetchrow(
                    "SELECT handler_role_id FROM guild_config WHERE guild_id=$1", interaction.guild_id
                )
            if cfg and cfg["handler_role_id"]:
                role = interaction.guild.get_role(cfg["handler_role_id"])
                if role:
                    for member in role.members:
                        try:
                            await member.remove_roles(role, reason="War reset by Command")
                        except Exception:
                            pass
        except Exception:
            pass

        await interaction.response.send_message(
            "🔄 War reset by Command. All data cleared. Citadel Hub A restored.", ephemeral=False)

    @app_commands.command(name="set_turn_interval", description="[Admin] Set hours between Legion advance turns.")
    @app_commands.describe(hours="Turn interval in hours (1–168)")
    async def set_turn_interval(self, interaction: discord.Interaction, hours: int):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Command staff only.", ephemeral=True)
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
        await interaction.response.send_message(
            f"✅ Legion advance interval set to **{hours}h**.", ephemeral=True)

    @app_commands.command(name="set_handler_role", description="[Admin] Set the role assigned to enlisted Handlers.")
    @app_commands.describe(role="The role to assign when a player enlists as a Handler")
    async def set_handler_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Command staff only.", ephemeral=True)
            return
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS handler_role_id BIGINT DEFAULT NULL"
            )
            await conn.execute(
                "UPDATE guild_config SET handler_role_id=$1 WHERE guild_id=$2",
                role.id, interaction.guild_id,
            )
        await interaction.response.send_message(
            f"✅ Handler role set to {role.mention}. "
            f"Players will receive this role upon enlistment.", ephemeral=True
        )

    @app_commands.command(name="game_status", description="View current war status across all sectors.")
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
        gm_text      = f"<@&{gm_role_id}>" if gm_role_id else "Not set — use `/set_gamemaster_role`"
        handler_role_id = config["handler_role_id"] if config and "handler_role_id" in config.keys() else None
        handler_text = f"<@&{handler_role_id}>" if handler_role_id else "Not set — use `/set_handler_role`"

        embed = discord.Embed(
            title="📊 War Status — Risk Universalis",
            color=discord.Color.blurple()
        )
        embed.add_field(name="War Status", value="🟢 Active" if config["game_started"] else "🔴 Paused", inline=True)
        embed.add_field(name="Advance Interval", value=f"{config['turn_interval_hours']}h", inline=True)
        embed.add_field(name="Turns Resolved", value=str(turn_count), inline=True)
        embed.add_field(name="Active Handlers", value=str(player_count), inline=True)
        embed.add_field(name="Active Legion Units", value=str(legion_count), inline=True)
        embed.add_field(name="Gamemaster Role", value=gm_text, inline=True)
        embed.add_field(name="Handler Role", value=handler_text, inline=True)
        embed.add_field(
            name="Outer Sector Status",
            value="\n".join(f"**{k}**: {v}" for k, v in hex_info.items()) or "No data",
            inline=False,
        )
        embed.set_footer(text=f"Last advance: {config['last_turn_at'].strftime('%Y-%m-%d %H:%M UTC')}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="set_report_channel", description="[Admin] Set the channel where turn reports are posted.")
    @app_commands.describe(channel="The text channel to post After Action Reports in")
    async def set_report_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Command staff only.", ephemeral=True)
            return
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_config SET report_channel_id=$1 WHERE guild_id=$2",
                channel.id, interaction.guild_id,
            )
        await interaction.response.send_message(
            f"✅ After Action Reports will be posted in {channel.mention}.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
