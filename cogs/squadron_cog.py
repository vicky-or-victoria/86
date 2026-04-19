import random
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import (
    OUTER_LABELS, SAFE_HUB, SAFE_HUB_DEPLOY, SUB_POSITIONS,
    outer_of, mid_of, level_of, inner_pos, mid_pos,
    is_edge_inner, adjacent_inner_clusters, can_cross_to_outer,
    entry_hex_for_outer,
    STATUS_LEGION, STATUS_MAJ_LEGION,
)

# ── Starter squadron definitions ──────────────────────────────────────────────
#
# Three archetypes, each totalling 60 stat points.
# They are designed to counter different Legion unit strengths:
#
#   VANGUARD  – Heavy assault. High Attack + Speed, low Supply + Recon.
#               Best against Löwe and Juggernaut (tank-type, high Defense).
#               Weakness: starves fast; blind to ambushes.
#
#   RECON     – Scout specialist. Very high Recon + Speed, low Attack + Defense.
#               Best against Shepherd (command unit, high Recon) and Grauwolf.
#               Weakness: fragile in direct combat.
#
#   FORTRESS  – Defensive anchor. Very high Defense + Supply + Morale, low Speed.
#               Best against Dinosauria (swarm type, high Attack volume).
#               Weakness: can't pursue or redeploy quickly.

STARTER_SQUADRONS = {
    "vanguard": {
        "label": "⚔️  Vanguard",
        "description": (
            "**Vanguard** — *Assault Specialists*\n"
            "Built for aggressive pushes into enemy territory. Excels at cracking heavily "
            "armoured Legion like **Löwe** and **Juggernaut**.\n\n"
            "**Strengths:** High Attack & Speed → strong initiative, hits hard first.\n"
            "**Weaknesses:** Low Supply & Recon — starves on long campaigns and can be "
            "ambushed by Shepherd command units.\n\n"
            "`ATK 16 | DEF 10 | SPD 14 | MOR 10 | SUP 5 | RCN 5`"
        ),
        "stats": {"attack": 16, "defense": 10, "speed": 14,
                  "morale": 10, "supply": 5,  "recon": 5},
    },
    "recon": {
        "label": "🔭  Recon",
        "description": (
            "**Recon** — *Scout Specialists*\n"
            "Fast, eyes-open, and elusive. Disrupts **Shepherd** command networks and "
            "outruns **Grauwolf** wolf-packs before they can surround you.\n\n"
            "**Strengths:** Very high Recon & Speed → enemy stats revealed before combat; "
            "superior initiative.\n"
            "**Weaknesses:** Low Attack & Defense — direct fights are risky. Avoid "
            "Juggernaut and Dinosauria swarms.\n\n"
            "`ATK 7 | DEF 7 | SPD 16 | MOR 10 | SUP 10 | RCN 20`  *(total: 70 — see note)*\n"
            "`ATK 6 | DEF 6 | SPD 16 | MOR 10 | SUP 10 | RCN 12`"
        ),
        "stats": {"attack": 6, "defense": 6, "speed": 16,
                  "morale": 10, "supply": 12, "recon": 10},
    },
    "fortress": {
        "label": "🛡️  Fortress",
        "description": (
            "**Fortress** — *Defensive Anchors*\n"
            "Digs in and holds the line. Built to absorb **Dinosauria** swarm attacks "
            "and outlast sustained Legion offensives.\n\n"
            "**Strengths:** Very high Defense, Supply & Morale → survives long fights; "
            "morale rerolls on bad dice; never starves.\n"
            "**Weaknesses:** Very low Speed — slow to redeploy and will lose initiative "
            "against fast Legion types like Grauwolf.\n\n"
            "`ATK 8 | DEF 18 | SPD 4 | MOR 14 | SUP 14 | RCN 2`"
        ),
        "stats": {"attack": 8, "defense": 18, "speed": 4,
                  "morale": 14, "supply": 14, "recon": 2},
    },
}

# Outer hexes the player can choose to deploy in (excludes Hex A = safe hub)
DEPLOYABLE_OUTERS = [o for o in OUTER_LABELS if o != SAFE_HUB]

# Legion-controlled status values that block deployment
_LEGION_STATUSES = {STATUS_LEGION, STATUS_MAJ_LEGION}


# ── Registration flow ─────────────────────────────────────────────────────────

class RegistrationView(discord.ui.View):
    """Persistent button embed posted by GMs. Any player can press Register."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋  Register as a Handler",
        style=discord.ButtonStyle.success,
        custom_id="sq_register_start",
    )
    async def register_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT deploy_hex FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
                interaction.guild_id, interaction.user.id,
            )
            game_row = await conn.fetchrow(
                "SELECT game_started FROM guild_config WHERE guild_id=$1", interaction.guild_id
            )

        if not game_row or not game_row["game_started"]:
            await interaction.response.send_message(
                "❌ The game hasn't started yet. Wait for a GM to use `/game_start`.",
                ephemeral=True,
            )
            return

        if existing:
            await interaction.response.send_message(
                f"❌ You're already registered. Your deploy hex is **{existing['deploy_hex']}**.",
                ephemeral=True,
            )
            return

        # Step 1 — choose squadron type
        embed = discord.Embed(
            title="🔰 Handler Registration — Step 1 of 2",
            description=(
                "Choose your **starter squadron type**. Each has different strengths and "
                "counters different Legion unit types.\n\n"
                + "\n\n─────────────────────────\n\n".join(
                    v["description"] for v in STARTER_SQUADRONS.values()
                )
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="86 — Eighty Six | Choose wisely. You cannot change this later.")
        view = SquadronTypeView(guild_id=interaction.guild_id, owner_id=interaction.user.id,
                                owner_name=interaction.user.display_name)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SquadronTypeView(discord.ui.View):
    """Step 1: player picks their squadron archetype."""
    def __init__(self, guild_id: int, owner_id: int, owner_name: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.owner_name = owner_name

        for key, data in STARTER_SQUADRONS.items():
            btn = discord.ui.Button(
                label=data["label"],
                style=discord.ButtonStyle.primary,
                custom_id=f"sqtype_{key}",
            )
            btn.callback = self._make_type_callback(key)
            self.add_item(btn)

    def _make_type_callback(self, squad_type: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ This isn't your registration.", ephemeral=True)
                return
            await _show_deploy_chooser(interaction, self.guild_id, self.owner_id,
                                       self.owner_name, squad_type)
        return callback


async def _show_deploy_chooser(
    interaction: discord.Interaction,
    guild_id: int,
    owner_id: int,
    owner_name: str,
    squad_type: str,
):
    """Step 2: show available outer hexes and let player choose a deploy zone."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        hex_rows = await conn.fetch(
            "SELECT address, status FROM hexes WHERE guild_id=$1 AND level=1 AND address != $2",
            guild_id, SAFE_HUB,
        )

    status_map = {r["address"]: r["status"] for r in hex_rows}

    available = []
    blocked = []
    for outer in DEPLOYABLE_OUTERS:
        st = status_map.get(outer, "neutral")
        if st in _LEGION_STATUSES:
            blocked.append((outer, st))
        else:
            available.append(outer)

    lines = []
    for outer in DEPLOYABLE_OUTERS:
        st = status_map.get(outer, "neutral")
        if st in _LEGION_STATUSES:
            label = "legion_controlled" if st == STATUS_LEGION else "majority_legion"
            lines.append(f"**Hex {outer}** — 🔴 {label.replace('_', ' ').title()} *(unavailable)*")
        else:
            friendly = st.replace("_", " ").title()
            lines.append(f"**Hex {outer}** — ⬛ {friendly}")

    chosen_data = STARTER_SQUADRONS[squad_type]
    stats = chosen_data["stats"]

    embed = discord.Embed(
        title="🔰 Handler Registration — Step 2 of 2",
        description=(
            f"Squadron type locked: **{chosen_data['label']}**\n"
            f"`ATK {stats['attack']} | DEF {stats['defense']} | SPD {stats['speed']} "
            f"| MOR {stats['morale']} | SUP {stats['supply']} | RCN {stats['recon']}`\n\n"
            "**Choose your deployment zone.**\n"
            "You will be randomly placed in a level-3 hex within that outer hex. "
            "Hexes that are **majority or fully Legion-controlled** cannot be chosen.\n\n"
            + "\n".join(lines)
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Your exact spawn point within the hex will be chosen randomly.")

    view = DeployZoneView(
        guild_id=guild_id, owner_id=owner_id, owner_name=owner_name,
        squad_type=squad_type, available_outers=available,
    )
    await interaction.response.edit_message(embed=embed, view=view)


class DeployZoneView(discord.ui.View):
    """Step 2: player picks one of the available outer hexes to deploy into."""
    def __init__(self, guild_id: int, owner_id: int, owner_name: str,
                 squad_type: str, available_outers: list[str]):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.squad_type = squad_type

        for i, outer in enumerate(available_outers):
            btn = discord.ui.Button(
                label=f"Hex {outer}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sqdeploy_{outer}",
                row=i // 4,
            )
            btn.callback = self._make_deploy_callback(outer)
            self.add_item(btn)

    def _make_deploy_callback(self, outer: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ This isn't your registration.", ephemeral=True)
                return
            await _finalize_registration(interaction, self.guild_id, self.owner_id,
                                         self.owner_name, self.squad_type, outer)
        return callback


async def _finalize_registration(
    interaction: discord.Interaction,
    guild_id: int,
    owner_id: int,
    owner_name: str,
    squad_type: str,
    chosen_outer: str,
):
    """Randomly assign a level-3 deploy hex in chosen_outer, avoiding Legion-heavy hexes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Re-check the outer hex hasn't been taken while the player was choosing
        outer_row = await conn.fetchrow(
            "SELECT status FROM hexes WHERE guild_id=$1 AND address=$2",
            guild_id, chosen_outer,
        )
        if outer_row and outer_row["status"] in _LEGION_STATUSES:
            await interaction.response.edit_message(
                content=(
                    f"❌ Hex **{chosen_outer}** was captured by the Legion while you were choosing. "
                    "Please start over."
                ),
                embed=None, view=None,
            )
            return

        # Fetch all level-3 hexes inside the chosen outer hex with their statuses
        inner_rows = await conn.fetch(
            "SELECT address, controller FROM hexes "
            "WHERE guild_id=$1 AND level=3 AND split_part(address,'-',1)=$2",
            guild_id, chosen_outer,
        )

        # Build candidate pool: prefer neutral/player, only fall back to others if needed
        neutral_player = [r["address"] for r in inner_rows
                          if r["controller"] in ("neutral", "players")]
        all_inner = [r["address"] for r in inner_rows]

        candidates = neutral_player if neutral_player else all_inner
        if not candidates:
            # Fallback: generate addresses directly (should not happen after ensure_hexes)
            candidates = [
                f"{chosen_outer}-{m}-{i}"
                for m in SUB_POSITIONS for i in SUB_POSITIONS
            ]

        deploy_hex = random.choice(candidates)
        home_outer = outer_of(deploy_hex)

        chosen_data = STARTER_SQUADRONS[squad_type]
        stats = chosen_data["stats"]
        squad_name = f"{owner_name}'s {squad_type.capitalize()}"

        # Guard: one registration per player
        existing = await conn.fetchrow(
            "SELECT id FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
            guild_id, owner_id,
        )
        if existing:
            await interaction.response.edit_message(
                content="❌ You already have a registered squadron.",
                embed=None, view=None,
            )
            return

        await conn.execute(
            """INSERT INTO squadrons
               (guild_id, owner_id, owner_name, name, hex_address, deploy_hex, home_outer,
                attack, defense, speed, morale, supply, recon)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            guild_id, owner_id, owner_name, squad_name, deploy_hex, deploy_hex, home_outer,
            stats["attack"], stats["defense"], stats["speed"],
            stats["morale"], stats["supply"], stats["recon"],
        )

    embed = discord.Embed(
        title="✅ Squadron Registered",
        description=(
            f"Welcome to the front, **{owner_name}**.\n\n"
            f"**Squadron:** {squad_name}\n"
            f"**Type:** {chosen_data['label']}\n"
            f"**Deployed to:** `{deploy_hex}` (Hex {chosen_outer})\n\n"
            f"`ATK {stats['attack']} | DEF {stats['defense']} | SPD {stats['speed']} "
            f"| MOR {stats['morale']} | SUP {stats['supply']} | RCN {stats['recon']}`\n\n"
            "Use `/squadron_status` to check your position and `/squadron_move` to advance."
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="86 — Eighty Six | The Legion never stops.")
    await interaction.response.edit_message(embed=embed, view=None)


# ── Cog ───────────────────────────────────────────────────────────────────────

class SquadronCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Re-register the persistent registration view so buttons survive restarts
        self.bot.add_view(RegistrationView())

    @app_commands.command(
        name="post_registration",
        description="[GM/Admin] Post the Handler registration embed to this channel.",
    )
    async def post_registration(self, interaction: discord.Interaction):
        """Posts the persistent registration embed. GM/admin only."""
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1",
                interaction.guild_id,
            )
        gm_role_id = config["gamemaster_role_id"] if config else None

        is_privileged = (
            interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator
            or (gm_role_id and any(r.id == gm_role_id for r in interaction.user.roles))
        )
        if not is_privileged:
            await interaction.response.send_message("❌ GMs and admins only.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Handler Registration",
            description=(
                "The front lines are expanding. Handlers are needed.\n\n"
                "Press the button below to register your squadron and choose your deployment zone. "
                "You will select a **squadron type** (each counters different Legion units) "
                "and a **target outer hex** — your exact spawn will be randomised within safe territory.\n\n"
                "⚠️ You may only register **once**. Choose carefully."
            ),
            color=discord.Color.from_rgb(180, 30, 30),
        )
        embed.set_footer(text="86 — Eighty Six | All units, stand by.")
        await interaction.response.send_message(embed=embed, view=RegistrationView())

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
                "You have no active squadrons. Ask a GM to post the registration embed to join.",
                ephemeral=True)
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
