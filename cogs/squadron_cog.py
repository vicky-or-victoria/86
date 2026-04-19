import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import (
    OUTER_LABELS, SAFE_HUB, SAFE_HUB_DEPLOY, SUB_POSITIONS,
    outer_of, mid_of, level_of, inner_pos, mid_pos,
    is_edge_inner, adjacent_inner_clusters, can_cross_to_outer,
    entry_hex_for_outer,
)

STAT_POINTS = 60


class StatModal(discord.ui.Modal, title="Configure Your Squadron Stats"):
    attack = discord.ui.TextInput(label="Attack (1–20)", default="10", max_length=2)
    defense = discord.ui.TextInput(label="Defense (1–20)", default="10", max_length=2)
    speed = discord.ui.TextInput(label="Speed (1–20)", default="10", max_length=2)
    morale = discord.ui.TextInput(label="Morale (1–20)", default="10", max_length=2)
    supply = discord.ui.TextInput(label="Supply (1–20)", default="10", max_length=2)
    recon = discord.ui.TextInput(label="Recon (1–20)", default="10", max_length=2)

    def __init__(self, name: str, deploy_hex: str, guild_id: int, owner_name: str):
        super().__init__()
        self.squadron_name = name
        self.deploy_hex = deploy_hex
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

            home = outer_of(self.deploy_hex)
            await conn.execute(
                """INSERT INTO squadrons
                   (guild_id, owner_id, owner_name, name, hex_address, deploy_hex, home_outer,
                    attack, defense, speed, morale, supply, recon)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                self.guild_id, interaction.user.id, self.owner_name,
                self.squadron_name, self.deploy_hex, self.deploy_hex, home,
                stats["attack"], stats["defense"], stats["speed"],
                stats["morale"], stats["supply"], stats["recon"],
            )

        embed = discord.Embed(
            title=f"✅ Squadron Registered: {self.squadron_name}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Deploy Hex", value=self.deploy_hex, inline=True)
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

    @app_commands.command(name="squadron_register", description="Register your squadron and choose a level-3 deploy hex in Hex A.")
    @app_commands.describe(
        name="Name of your squadron",
        deploy_hex="Your permanent level-3 deploy point inside Hex A (e.g. A-C-1, A-3-4). Locked permanently.",
    )
    async def register(self, interaction: discord.Interaction, name: str, deploy_hex: str):
        await ensure_guild(interaction.guild_id)
        deploy_hex = deploy_hex.strip().upper()

        # Validate: must be a level-3 hex inside Hex A
        if level_of(deploy_hex) != 3 or outer_of(deploy_hex) != SAFE_HUB:
            await interaction.response.send_message(
                f"❌ Deploy hex must be a level-3 hex inside Hex A "
                f"(e.g. `A-C-1`, `A-3-4`, `A-C-C`). Got: `{deploy_hex}`",
                ephemeral=True)
            return

        parts = deploy_hex.split("-")
        if len(parts) != 3 or parts[1] not in SUB_POSITIONS or parts[2] not in SUB_POSITIONS:
            await interaction.response.send_message(
                f"❌ Invalid hex address `{deploy_hex}`. Format: A-<mid>-<inner> "
                f"where mid and inner are each C, 1, 2, 3, 4, 5, or 6.",
                ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Each player registers once only
            existing = await conn.fetchrow(
                "SELECT deploy_hex FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
                interaction.guild_id, interaction.user.id
            )
            if existing:
                await interaction.response.send_message(
                    f"❌ You already have a registered squadron. Each player registers once. "
                    f"Your deploy hex is **{existing['deploy_hex']}**.",
                    ephemeral=True)
                return

        owner_name = interaction.user.display_name
        modal = StatModal(name=name, deploy_hex=deploy_hex,
                          guild_id=interaction.guild_id, owner_name=owner_name)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="squadron_move", description="Move your squadron to a level-3 hex.")
    @app_commands.describe(
        squadron_name="Name of your squadron",
        address="Target level-3 hex (e.g. B-2-4). Same cluster=instant. Adjacent cluster (edge only)=1 turn. Different outer (corner only)=2 turns via Hub A.",
    )
    async def move(self, interaction: discord.Interaction, squadron_name: str, address: str):
        address = address.strip().upper()
        await ensure_guild(interaction.guild_id)

        if level_of(address) != 3:
            await interaction.response.send_message(
                "❌ You must move to a level-3 hex (e.g. `B-2-4` or `A-C-1`).",
                ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            sq = await conn.fetchrow(
                "SELECT id, name, hex_address, home_outer, in_transit, deploy_hex FROM squadrons "
                "WHERE guild_id=$1 AND owner_id=$2 AND name=$3 AND is_active=TRUE",
                interaction.guild_id, interaction.user.id, squadron_name
            )
            if not sq:
                await interaction.response.send_message(
                    f"❌ No active squadron named **{squadron_name}**.", ephemeral=True)
                return

            if sq["in_transit"]:
                await interaction.response.send_message(
                    "❌ This squadron is already in transit. Wait for the turn to resolve.",
                    ephemeral=True)
                return

            hex_exists = await conn.fetchrow(
                "SELECT address FROM hexes WHERE guild_id=$1 AND address=$2",
                interaction.guild_id, address
            )
            if not hex_exists:
                await interaction.response.send_message(
                    f"❌ Hex `{address}` doesn't exist.", ephemeral=True)
                return

            current = sq["hex_address"]
            current_mid = mid_of(current)
            current_outer = outer_of(current)
            target_mid = mid_of(address)
            target_outer = outer_of(address)

            # ── Case 1: Same cluster — instant free move ──────────────────────
            if current_mid == target_mid:
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1 WHERE id=$2",
                    address, sq["id"]
                )
                await interaction.response.send_message(
                    f"📡 **{squadron_name}** moved to **{address}**.")
                return

            # ── Case 2: Adjacent cluster within same outer hex ────────────────
            if target_outer == current_outer:
                if not is_edge_inner(current):
                    await interaction.response.send_message(
                        f"❌ **{squadron_name}** is at `{current}` (center position `C`). "
                        f"Move to an edge inner hex (position 1–6) within your cluster first.",
                        ephemeral=True)
                    return
                reachable = adjacent_inner_clusters(current)
                if target_mid not in reachable:
                    reachable_str = ", ".join(reachable) if reachable else "none from this position"
                    await interaction.response.send_message(
                        f"❌ Cannot reach `{target_mid}` from `{current}`. "
                        f"Reachable adjacent clusters: {reachable_str}.",
                        ephemeral=True)
                    return
                # 1-turn transit — land at center of target cluster
                entry = f"{target_mid}-C"
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2 "
                    "WHERE id=$2",
                    entry, sq["id"]
                )
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** crossing to cluster **{target_mid}**, "
                    f"arriving at `{entry}` next turn.")
                return

            # ── Case 3: Different outer hex — inter-outer transit ─────────────
            crossable_outer = can_cross_to_outer(current)
            if crossable_outer is None:
                await interaction.response.send_message(
                    f"❌ **{squadron_name}** at `{current}` cannot cross to another outer hex. "
                    f"You must be at a corner hex where mid pos = inner pos (e.g. `B-2-2`) "
                    f"to initiate inter-outer transit.",
                    ephemeral=True)
                return

            if current_outer == SAFE_HUB:
                # Already in A — one step out to destination
                entry = entry_hex_for_outer(target_outer, SAFE_HUB)
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2, "
                    "home_outer=$2 WHERE id=$3",
                    entry, target_outer, sq["id"]
                )
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** deploying from Hub A to **Hex {target_outer}**, "
                    f"arriving at `{entry}` next turn.")
            elif target_outer == SAFE_HUB:
                # Withdrawing to A — one step
                entry = entry_hex_for_outer(SAFE_HUB, current_outer)
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2, "
                    "home_outer=$2 WHERE id=$3",
                    entry, SAFE_HUB, sq["id"]
                )
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** withdrawing to **Hub A**, "
                    f"arriving at `{entry}` next turn.")
            else:
                # Two-step: current outer → A → destination
                hub_entry = entry_hex_for_outer(SAFE_HUB, current_outer)
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=1, "
                    "home_outer=$2 WHERE id=$3",
                    target_outer, SAFE_HUB, sq["id"]
                )
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** begins transit: "
                    f"**{current_outer} → Hub A → {target_outer}**. "
                    f"Step 1: arrive at `{hub_entry}` next turn. Takes 2 turns total.")

    @app_commands.command(name="squadron_status", description="View your squadron stats and location.")
    async def status(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
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
                    f"Home: **{s['home_outer']}** | Deploy: **{s['deploy_hex'] or 'N/A'}**"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SquadronCog(bot))
