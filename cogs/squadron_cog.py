import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import OUTER_LABELS, SAFE_HUB, outer_of

STAT_POINTS = 60


class StatModal(discord.ui.Modal, title="Configure Your Squadron Stats"):
    attack = discord.ui.TextInput(label="Attack (1–20)", default="10", max_length=2)
    defense = discord.ui.TextInput(label="Defense (1–20)", default="10", max_length=2)
    speed = discord.ui.TextInput(label="Speed (1–20)", default="10", max_length=2)
    morale = discord.ui.TextInput(label="Morale (1–20)", default="10", max_length=2)
    supply = discord.ui.TextInput(label="Supply (1–20)", default="10", max_length=2)
    recon = discord.ui.TextInput(label="Recon (1–20)", default="10", max_length=2)

    def __init__(self, name: str, hex_address: str, guild_id: int, owner_name: str):
        super().__init__()
        self.squadron_name = name
        self.hex_address = hex_address
        self.guild_id = guild_id
        self.owner_name = owner_name

    async def on_submit(self, interaction: discord.Interaction):
        try:
            stats = {k: int(getattr(self, k).value) for k in
                     ["attack", "defense", "speed", "morale", "supply", "recon"]}
        except ValueError:
            await interaction.response.send_message("❌ Stats must be whole numbers.", ephemeral=True)
            return

        for k, v in stats.items():
            if not (1 <= v <= 20):
                await interaction.response.send_message(
                    f"❌ {k.capitalize()} must be between 1 and 20.", ephemeral=True)
                return

        if sum(stats.values()) != STAT_POINTS:
            await interaction.response.send_message(
                f"❌ Stats must total exactly **{STAT_POINTS}**. Yours total {sum(stats.values())}.",
                ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND name=$3",
                self.guild_id, interaction.user.id, self.squadron_name,
            )
            if existing:
                await interaction.response.send_message(
                    "❌ You already have a squadron with that name.", ephemeral=True)
                return

            home = outer_of(self.hex_address)
            await conn.execute(
                """INSERT INTO squadrons
                   (guild_id, owner_id, owner_name, name, hex_address, home_outer,
                    attack, defense, speed, morale, supply, recon)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                self.guild_id, interaction.user.id, self.owner_name,
                self.squadron_name, self.hex_address, home,
                stats["attack"], stats["defense"], stats["speed"],
                stats["morale"], stats["supply"], stats["recon"],
            )

        embed = discord.Embed(
            title=f"✅ Squadron Registered: {self.squadron_name}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Location", value=self.hex_address, inline=True)
        embed.add_field(name="Commander", value=self.owner_name, inline=True)
        embed.add_field(
            name="Stats",
            value="\n".join(f"**{k.capitalize()}**: {v}" for k, v in stats.items()),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)


class SquadronCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="squadron_register", description="Register your squadron.")
    @app_commands.describe(
        name="Name of your squadron",
        hex_address="Starting outer hex (A–G). All squadrons start on an outer hex.",
    )
    async def register(self, interaction: discord.Interaction, name: str, hex_address: str):
        await ensure_guild(interaction.guild_id)
        hex_address = hex_address.strip().upper()
        if hex_address not in OUTER_LABELS:
            await interaction.response.send_message(
                f"❌ Starting hex must be an outer hex: {', '.join(OUTER_LABELS)}", ephemeral=True)
            return

        # Check if player already has squadrons — must match existing home outer hex
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT home_outer FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
                interaction.guild_id, interaction.user.id
            )
            if existing and existing["home_outer"] != hex_address:
                await interaction.response.send_message(
                    f"❌ All your squadrons must start in the same outer hex. "
                    f"Your home hex is **{existing['home_outer']}**.", ephemeral=True)
                return

        owner_name = interaction.user.display_name
        modal = StatModal(name=name, hex_address=hex_address,
                          guild_id=interaction.guild_id, owner_name=owner_name)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="squadron_move", description="Move your squadron within your outer hex or begin transit.")
    @app_commands.describe(
        squadron_name="Name of your squadron",
        address="Target hex address (within your outer hex, or a new outer hex to travel to)",
    )
    async def move(self, interaction: discord.Interaction, squadron_name: str, address: str):
        address = address.strip().upper()
        pool = await get_pool()
        async with pool.acquire() as conn:
            sq = await conn.fetchrow(
                "SELECT id, name, hex_address, home_outer, in_transit FROM squadrons "
                "WHERE guild_id=$1 AND owner_id=$2 AND name=$3 AND is_active=TRUE",
                interaction.guild_id, interaction.user.id, squadron_name
            )
            if not sq:
                await interaction.response.send_message(
                    f"❌ No active squadron named **{squadron_name}**.", ephemeral=True)
                return

            if sq["in_transit"]:
                await interaction.response.send_message(
                    "❌ This squadron is already in transit. Wait for the turn to resolve.", ephemeral=True)
                return

            hex_exists = await conn.fetchrow(
                "SELECT address, level FROM hexes WHERE guild_id=$1 AND address=$2",
                interaction.guild_id, address
            )
            if not hex_exists:
                await interaction.response.send_message(f"❌ Hex `{address}` doesn't exist.", ephemeral=True)
                return

            target_outer = outer_of(address)
            home_outer = sq["home_outer"]

            # Same outer hex — free movement within level 2/3
            if target_outer == home_outer or address == home_outer:
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1 WHERE id=$2",
                    address, sq["id"]
                )
                await interaction.response.send_message(
                    f"📡 **{squadron_name}** moved to **{address}**.")
                return

            # Moving to a different outer hex — start transit
            if address not in OUTER_LABELS:
                await interaction.response.send_message(
                    "❌ To travel to another outer hex, specify the outer hex label (e.g. `C`).", ephemeral=True)
                return

            if address == SAFE_HUB:
                # Travelling to A directly — one step
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=1 WHERE id=$2",
                    SAFE_HUB, sq["id"]
                )
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** is heading to **Hex A**. Will arrive next turn.")
            else:
                if home_outer == SAFE_HUB:
                    # Already at A — one step to destination
                    await conn.execute(
                        "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2 WHERE id=$2",
                        address, sq["id"]
                    )
                    await interaction.response.send_message(
                        f"🚶 **{squadron_name}** is heading to **Hex {address}**. Will arrive next turn.")
                else:
                    # Two steps: current outer → A → destination
                    await conn.execute(
                        "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=1 WHERE id=$2",
                        address, sq["id"]
                    )
                    await interaction.response.send_message(
                        f"🚶 **{squadron_name}** begins transit: **{home_outer} → A → {address}**. Takes 2 turns.")

    @app_commands.command(name="squadron_status", description="View your squadron stats and location.")
    async def status(self, interaction: discord.Interaction):
        pool = await get_pool()
        async with pool.acquire() as conn:
            squadrons = await conn.fetch(
                "SELECT * FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE",
                interaction.guild_id, interaction.user.id,
            )

        if not squadrons:
            await interaction.response.send_message(
                "You have no active squadrons. Use `/squadron_register` to join.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 {interaction.user.display_name}'s Squadrons",
            color=discord.Color.gold())
        for s in squadrons:
            transit_info = ""
            if s["in_transit"]:
                transit_info = f"\n✈️ *In transit to {s['transit_destination']} (step {s['transit_step']}/2)*"
            embed.add_field(
                name=f"🔰 {s['name']} — {s['hex_address']}{transit_info}",
                value=(
                    f"ATK `{s['attack']}` | DEF `{s['defense']}` | SPD `{s['speed']}`\n"
                    f"MOR `{s['morale']}` | SUP `{s['supply']}` | RCN `{s['recon']}`\n"
                    f"Home Outer: **{s['home_outer']}**"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SquadronCog(bot))
