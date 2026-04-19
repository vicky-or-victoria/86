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
# They counter different Legion unit strengths.

STARTER_SQUADRONS = {
    "vanguard": {
        "label": "⚔️  Vanguard",
        "short": "Vanguard",
        "description": (
            "**⚔️ Vanguard** — *Assault Numbers*\n"
            "Built for aggressive pushes through Legion-controlled territory. "
            "Excels at cracking armoured Legion like **Löwe** and **Juggernaut**.\n"
            "> **Strengths:** High ATK & SPD — hits hard and strikes first.\n"
            "> **Weaknesses:** Low SUP & RCN — starves fast; blind to Shepherd ambushes.\n"
            "> `ATK 16 | DEF 10 | SPD 14 | MOR 10 | SUP 5 | RCN 5`"
        ),
        "stats": {"attack": 16, "defense": 10, "speed": 14,
                  "morale": 10, "supply": 5,  "recon": 5},
    },
    "recon": {
        "label": "🔭  Recon",
        "short": "Recon",
        "description": (
            "**🔭 Recon** — *Scout Numbers*\n"
            "Fast and elusive across the front lines. Disrupts **Shepherd** "
            "command networks and outruns **Grauwolf** wolf-packs.\n"
            "> **Strengths:** Very high RCN & SPD — reveals enemy stats; superior initiative.\n"
            "> **Weaknesses:** Low ATK & DEF — avoid Juggernaut and Dinosauria swarms.\n"
            "> `ATK 6 | DEF 6 | SPD 16 | MOR 10 | SUP 12 | RCN 10`"
        ),
        "stats": {"attack": 6, "defense": 6, "speed": 16,
                  "morale": 10, "supply": 12, "recon": 10},
    },
    "fortress": {
        "label": "🛡️  Fortress",
        "short": "Fortress",
        "description": (
            "**🛡️ Fortress** — *Defensive Numbers*\n"
            "Digs in and holds the border line. Built to absorb **Dinosauria** "
            "swarm attacks and outlast sustained Legion offensives.\n"
            "> **Strengths:** Very high DEF, SUP & MOR — survives long fights; morale rerolls.\n"
            "> **Weaknesses:** Very low SPD — slow to redeploy; loses initiative to Grauwolf.\n"
            "> `ATK 8 | DEF 18 | SPD 4 | MOR 14 | SUP 14 | RCN 2`"
        ),
        "stats": {"attack": 8, "defense": 18, "speed": 4,
                  "morale": 14, "supply": 14, "recon": 2},
    },
}

DEPLOYABLE_OUTERS = [o for o in OUTER_LABELS if o != SAFE_HUB]
_LEGION_STATUSES = {STATUS_LEGION, STATUS_MAJ_LEGION}

# Tracks the posted registration message per guild for counter updates.
# { guild_id: {"channel_id": int, "message_id": int} }
_registration_messages: dict[int, dict] = {}


async def update_registration_embed(bot, guild_id: int):
    """Edit the live registration embed to show the current Handler count."""
    if guild_id not in _registration_messages:
        return
    info = _registration_messages[guild_id]
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    channel = guild.get_channel(info["channel_id"])
    if not channel:
        return
    try:
        message = await channel.fetch_message(info["message_id"])
    except discord.NotFound:
        _registration_messages.pop(guild_id, None)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(DISTINCT owner_id) FROM squadrons WHERE guild_id=$1 AND is_active=TRUE",
            guild_id,
        )
        outer_rows = await conn.fetch(
            "SELECT status FROM hexes WHERE guild_id=$1 AND level=1 AND address != $2",
            guild_id, SAFE_HUB,
        )
    all_legion = all(r["status"] in _LEGION_STATUSES for r in outer_rows) if outer_rows else False

    embed = _build_registration_embed(count, all_legion)
    await message.edit(embed=embed, view=RegistrationView() if not all_legion else discord.ui.View())


def _build_registration_embed(player_count: int, endgame: bool) -> discord.Embed:
    if endgame:
        embed = discord.Embed(
            title="☠️ The Citadel Has Fallen",
            description=(
                "**The Legion controls all border sectors. Risk Universalis has been overrun.**\n\n"
                "Handler enlistment is closed. The war is lost.\n"
                "A full command reset is required before new Handlers can be assigned.\n\n"
                f"**Handlers who fought:** `{player_count}`"
            ),
            color=discord.Color.from_rgb(80, 0, 0),
        )
    else:
        embed = discord.Embed(
            title="📋 Handler Enlistment — Squadron 86",
            description=(
                "**Risk Universalis is under siege.**\n\n"
                "The Legion AI hivemind advances across our borders with no signs of stopping. "
                "Numbered squadrons — unmanned spider mechs piloted remotely by Handlers "
                "from the safety of the capital citadel — are our last line of defense.\n\n"
                "Press **Enlist as Handler** to choose your Number type "
                "and deployment sector. Your exact spawn will be randomised within safe territory.\n\n"
                "⚠️ You may only enlist **once per war**. Choose carefully.\n\n"
                f"**Active Handlers:** `{player_count}`"
            ),
            color=discord.Color.from_rgb(180, 30, 30),
        )
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | All Numbers, stand by.")
    return embed


# ── Registration UI flow ──────────────────────────────────────────────────────

class RegistrationView(discord.ui.View):
    """Persistent button posted by GMs. Any player can press Enlist."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋  Enlist as Handler",
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
            outer_rows = await conn.fetch(
                "SELECT status FROM hexes WHERE guild_id=$1 AND level=1 AND address != $2",
                interaction.guild_id, SAFE_HUB,
            )

        if not game_row or not game_row["game_started"]:
            await interaction.response.send_message(
                "❌ The war hasn't begun yet. Wait for Command to issue `/game_start`.",
                ephemeral=True,
            )
            return

        # Endgame lock — all outer hexes B–G are legion-controlled
        if outer_rows and all(r["status"] in _LEGION_STATUSES for r in outer_rows):
            await interaction.response.send_message(
                "☠️ **Enlistment is closed.** The Legion controls all border sectors — "
                "Risk Universalis has fallen. A full command reset is required before new Handlers can be assigned.",
                ephemeral=True,
            )
            return

        if existing:
            await interaction.response.send_message(
                f"❌ You're already enlisted. Your Number is deployed at **{existing['deploy_hex']}**.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔰 Handler Enlistment — Step 1 of 2: Choose Your Number Type",
            description=(
                "Select a **Number type** for your spider mech squadron. "
                "Each archetype is designed to counter different Legion unit types.\n\n"
                + "\n\n─────────────────────\n\n".join(
                    v["description"] for v in STARTER_SQUADRONS.values()
                )
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Risk Universalis 3 — Squadron 86 | Choose wisely — this cannot be changed.")
        view = SquadronTypeView(guild_id=interaction.guild_id, owner_id=interaction.user.id,
                                owner_name=interaction.user.display_name)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SquadronTypeView(discord.ui.View):
    def __init__(self, guild_id: int, owner_id: int, owner_name: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.owner_name = owner_name
        for key, data in STARTER_SQUADRONS.items():
            btn = discord.ui.Button(label=data["label"], style=discord.ButtonStyle.primary,
                                    custom_id=f"sqtype_{key}")
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, squad_type: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ This isn't your enlistment form.", ephemeral=True)
                return
            await _show_deploy_chooser(interaction, self.guild_id, self.owner_id,
                                       self.owner_name, squad_type)
        return callback


async def _show_deploy_chooser(interaction, guild_id, owner_id, owner_name, squad_type):
    pool = await get_pool()
    async with pool.acquire() as conn:
        hex_rows = await conn.fetch(
            "SELECT address, status FROM hexes WHERE guild_id=$1 AND level=1 AND address != $2",
            guild_id, SAFE_HUB,
        )
    status_map = {r["address"]: r["status"] for r in hex_rows}
    available = [o for o in DEPLOYABLE_OUTERS if status_map.get(o, "neutral") not in _LEGION_STATUSES]

    lines = []
    for outer in DEPLOYABLE_OUTERS:
        st = status_map.get(outer, "neutral")
        if st in _LEGION_STATUSES:
            label = "Legion Controlled" if st == STATUS_LEGION else "Legion Majority"
            lines.append(f"🔴 **Sector {outer}** — {label} *(unavailable)*")
        else:
            friendly = st.replace("_", " ").title()
            lines.append(f"⬛ **Sector {outer}** — {friendly}")

    chosen_data = STARTER_SQUADRONS[squad_type]
    stats = chosen_data["stats"]
    embed = discord.Embed(
        title="🔰 Handler Enlistment — Step 2 of 2: Choose Deployment Sector",
        description=(
            f"**Number Type:** {chosen_data['label']}\n"
            f"> `ATK {stats['attack']} | DEF {stats['defense']} | SPD {stats['speed']}"
            f" | MOR {stats['morale']} | SUP {stats['supply']} | RCN {stats['recon']}`\n\n"
            "Choose your **border sector** for deployment. Your exact level-3 position will be "
            "randomly assigned within available (non-Legion) territory.\n\n"
            + "\n".join(lines)
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Legion-controlled sectors are unavailable for deployment.")
    view = DeployZoneView(guild_id=guild_id, owner_id=owner_id, owner_name=owner_name,
                          squad_type=squad_type, available_outers=available)
    await interaction.response.edit_message(embed=embed, view=view)


class DeployZoneView(discord.ui.View):
    def __init__(self, guild_id, owner_id, owner_name, squad_type, available_outers):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.squad_type = squad_type
        for i, outer in enumerate(available_outers):
            btn = discord.ui.Button(label=f"Sector {outer}", style=discord.ButtonStyle.secondary,
                                    custom_id=f"sqdeploy_{outer}", row=i // 4)
            btn.callback = self._make_callback(outer)
            self.add_item(btn)

    def _make_callback(self, outer: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ This isn't your enlistment form.", ephemeral=True)
                return
            await _finalize_registration(interaction, self.guild_id, self.owner_id,
                                         self.owner_name, self.squad_type, outer)
        return callback


async def _finalize_registration(interaction, guild_id, owner_id, owner_name, squad_type, chosen_outer):
    pool = await get_pool()
    async with pool.acquire() as conn:
        outer_row = await conn.fetchrow(
            "SELECT status FROM hexes WHERE guild_id=$1 AND address=$2", guild_id, chosen_outer)
        if outer_row and outer_row["status"] in _LEGION_STATUSES:
            await interaction.response.edit_message(
                content=f"❌ Sector **{chosen_outer}** was overrun while you were choosing. Please start over.",
                embed=None, view=None)
            return

        inner_rows = await conn.fetch(
            "SELECT address, controller FROM hexes "
            "WHERE guild_id=$1 AND level=3 AND split_part(address,'-',1)=$2",
            guild_id, chosen_outer,
        )
        neutral_player = [r["address"] for r in inner_rows if r["controller"] in ("neutral", "players")]
        all_inner = [r["address"] for r in inner_rows]
        candidates = neutral_player if neutral_player else all_inner
        if not candidates:
            candidates = [f"{chosen_outer}-{m}-{i}" for m in SUB_POSITIONS for i in SUB_POSITIONS]

        deploy_hex = random.choice(candidates)
        home_outer = outer_of(deploy_hex)
        chosen_data = STARTER_SQUADRONS[squad_type]
        stats = chosen_data["stats"]
        squad_name = f"{owner_name}'s {chosen_data['short']}"

        existing = await conn.fetchrow(
            "SELECT id FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
            guild_id, owner_id)
        if existing:
            await interaction.response.edit_message(
                content="❌ You already have an enlisted Number.", embed=None, view=None)
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
        title="✅ Enlistment Confirmed — Welcome to Squadron 86",
        description=(
            f"Your Number has been assigned, Handler **{owner_name}**.\n\n"
            f"**Squadron:** {squad_name}\n"
            f"**Type:** {chosen_data['label']}\n"
            f"**Deployed to:** `{deploy_hex}` *(Sector {chosen_outer})*\n\n"
            f"> `ATK {stats['attack']} | DEF {stats['defense']} | SPD {stats['speed']}"
            f" | MOR {stats['morale']} | SUP {stats['supply']} | RCN {stats['recon']}`\n\n"
            "Open the **Command Terminal** to check your Number's status or reposition your squadron.\n\n"
            "*The Legion never rests, Handler. Neither should you.*"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | The front lines await.")
    await interaction.response.edit_message(embed=embed, view=None)

    # Assign the handler role if configured
    try:
        pool2 = await get_pool()
        async with pool2.acquire() as conn2:
            cfg = await conn2.fetchrow(
                "SELECT handler_role_id FROM guild_config WHERE guild_id=$1", guild_id
            )
        if cfg and cfg["handler_role_id"]:
            role = interaction.guild.get_role(cfg["handler_role_id"])
            if role:
                await interaction.user.add_roles(role, reason="Handler enlistment")
    except Exception:
        pass

    # Update the live registration counter
    try:
        from cogs.squadron_cog import update_registration_embed
        await update_registration_embed(interaction.client, guild_id)
    except Exception:
        pass


# ── Command Terminal (HQ) Player Menu ─────────────────────────────────────────

class HQView(discord.ui.View):
    """
    Persistent embed menu posted by GMs via /post_hq.
    All buttons respond ephemerally — the live embed stays clean.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 My Number", style=discord.ButtonStyle.primary,
                       custom_id="hq_status", row=0)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _hq_status(interaction)

    @discord.ui.button(label="🚀 Reposition", style=discord.ButtonStyle.secondary,
                       custom_id="hq_move", row=0)
    async def move_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _hq_move_prompt(interaction)

    @discord.ui.button(label="🗺️ Front Lines", style=discord.ButtonStyle.secondary,
                       custom_id="hq_map", row=0)
    async def map_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _hq_map(interaction)

    @discord.ui.button(label="🎒 Scavenge", style=discord.ButtonStyle.secondary,
                       custom_id="hq_scavenge", row=1)
    async def scavenge_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _hq_scavenge(interaction)

    @discord.ui.button(label="📖 Field Manual", style=discord.ButtonStyle.gray,
                       custom_id="hq_help", row=1)
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _hq_help(interaction)


# ── HQ button handlers ────────────────────────────────────────────────────────

async def _hq_status(interaction: discord.Interaction):
    await ensure_guild(interaction.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        squadrons = await conn.fetch(
            "SELECT * FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE",
            interaction.guild_id, interaction.user.id,
        )
    if not squadrons:
        embed = discord.Embed(
            title="📋 No Number Assigned",
            description="You haven't enlisted yet. Use the **Handler Enlistment** embed to register your Number.",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📋 {interaction.user.display_name}'s Numbers",
        color=discord.Color.gold(),
    )
    for s in squadrons:
        transit_info = ""
        if s["in_transit"]:
            transit_info = f"\n> ✈️ *In transit to `{s['transit_destination']}` (step {s['transit_step']}/2)*"
        embed.add_field(
            name=f"🔰 {s['name']}",
            value=(
                f"**Position:** `{s['hex_address']}`{transit_info}\n"
                f"**Home Sector:** {s['home_outer']} | **Deploy Point:** `{s['deploy_hex'] or 'N/A'}`\n"
                f"> `ATK {s['attack']} | DEF {s['defense']} | SPD {s['speed']}"
                f" | MOR {s['morale']} | SUP {s['supply']} | RCN {s['recon']}`"
            ),
            inline=False,
        )
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | Use 🚀 Reposition to redeploy.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _hq_move_prompt(interaction: discord.Interaction):
    await ensure_guild(interaction.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        squadrons = await conn.fetch(
            "SELECT id, name, hex_address, in_transit FROM squadrons "
            "WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE",
            interaction.guild_id, interaction.user.id,
        )
    if not squadrons:
        await interaction.response.send_message(
            "❌ You have no active Number. Enlist first.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🚀 Reposition Number",
        description=(
            "To reposition your Number, use the slash command:\n"
            "```\n/squadron_move squadron_name:<n> address:<sector>\n```\n"
            "**Movement Rules:**\n"
            "> • **Same cluster** (e.g. `B-2-C` → `B-2-4`) — **instant**\n"
            "> • **Adjacent cluster** (edge pos 1–6 only) — **1 turn transit**\n"
            "> • **Different sector** (corner pos X-N-N only) — **2 turns via Citadel Hub A**\n\n"
            "**Your Numbers:**\n"
            + "\n".join(
                f"> `{s['name']}` @ `{s['hex_address']}`"
                + (" *(in transit)*" if s["in_transit"] else "")
                for s in squadrons
            )
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Tip: You must be at an edge position to cross clusters or sectors.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _hq_map(interaction: discord.Interaction):
    """Send the Handler an ephemeral front-line map snapshot."""
    await ensure_guild(interaction.guild_id)
    try:
        from cogs.map_cog import _fetch_map_data, _build_map_embed_and_file, EphemeralDrillView
    except ImportError:
        await interaction.response.send_message("❌ Map system unavailable.", ephemeral=True)
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        hexes, render_level, squadrons, legion_units = await _fetch_map_data(conn, interaction.guild_id, None)
    if not hexes:
        await interaction.response.send_message(
            "⚠️ No map data yet. Await Command to start the war.", ephemeral=True)
        return
    embed, map_file = _build_map_embed_and_file(hexes, render_level, squadrons, legion_units, None)
    view = EphemeralDrillView(guild_id=interaction.guild_id, address=None, render_level=1)
    await interaction.response.send_message(embed=embed, file=map_file, view=view, ephemeral=True)


async def _hq_scavenge(interaction: discord.Interaction):
    """
    Attempt to scavenge supply from the current sector.
    Only usable outside the Citadel Hub A.
    Limited to ONE attempt per turn per Handler.
      - Roll 4–6: gain 2 supply (capped at 20).
      - Roll 1–3: nothing found.
    """
    SCAVENGE_CAP  = 20
    SCAVENGE_GAIN = 2
    await ensure_guild(interaction.guild_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        sq = await conn.fetchrow(
            "SELECT id, name, hex_address, supply, last_scavenged_turn FROM squadrons "
            "WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
            interaction.guild_id, interaction.user.id,
        )
        if not sq:
            await interaction.response.send_message(
                "❌ You have no active Number.", ephemeral=True)
            return

        from utils.hexmap import outer_of, SAFE_HUB
        if outer_of(sq["hex_address"]) == SAFE_HUB:
            await interaction.response.send_message(
                "❌ Nothing to scavenge within the Citadel. Deploy to the front lines first.",
                ephemeral=True)
            return

        # Derive current turn number the same way the turn engine does
        current_turn = await conn.fetchval(
            "SELECT COUNT(*) FROM turn_history WHERE guild_id=$1",
            interaction.guild_id,
        )
        last_scavenged = sq["last_scavenged_turn"] if sq["last_scavenged_turn"] is not None else -1

        if last_scavenged >= current_turn:
            await interaction.response.send_message(
                "⏳ **Scavenge on cooldown.** Your Number has already swept this sector this turn.\n"
                "> One scavenge attempt is allowed per Legion advance.",
                ephemeral=True)
            return

        roll = random.randint(1, 6)
        if roll >= 4:
            new_supply = min(SCAVENGE_CAP, sq["supply"] + SCAVENGE_GAIN)
            gained     = new_supply - sq["supply"]
            await conn.execute(
                "UPDATE squadrons SET supply=$1, last_scavenged_turn=$2 WHERE id=$3",
                new_supply, current_turn, sq["id"]
            )
            embed = discord.Embed(
                title="🎒 Scavenge — Supplies Recovered",
                description=(
                    f"**{sq['name']}** recovered usable parts at `{sq['hex_address']}`.\n"
                    f"> Supply: `{sq['supply']}` → `{new_supply}` (+{gained})\n\n"
                    f"*Next scavenge available after the Legion's next advance.*"
                ),
                color=discord.Color.green(),
            )
        else:
            await conn.execute(
                "UPDATE squadrons SET last_scavenged_turn=$1 WHERE id=$2",
                current_turn, sq["id"]
            )
            embed = discord.Embed(
                title="🎒 Scavenge — Nothing Found",
                description=(
                    f"**{sq['name']}** swept `{sq['hex_address']}` but the sector was picked clean.\n"
                    f"> Supply remains at `{sq['supply']}`.\n\n"
                    f"*Next scavenge available after the Legion's next advance.*"
                ),
                color=discord.Color.dark_gray(),
            )
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | The front provides, or it doesn't.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _hq_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Handler Field Manual — Squadron 86",
        color=discord.Color.from_rgb(60, 60, 90),
        description=(
            "## The War\n"
            "It is **2086 AD**. For sixty years, Risk Universalis has held the line against "
            "**The Legion** — an AI hivemind that controls spider mech tanks with terrifying "
            "coordination. You are a **Handler**: a remote pilot commanding an unmanned "
            "**Number** from the safety of the capital citadel, while your machine fights on "
            "the front lines. The Legion has grown stronger. Numbered squadrons grow fewer. "
            "Squadron **86** is newly formed — and it falls to you.\n\n"
            "## The Map\n"
            "> The continent is divided into **Sectors** (A–G), each split into "
            "**7 mid-clusters**, each split into **7 inner positions**. "
            "Your Number operates at a **level-3** (inner) position at all times.\n"
            "> **Sector A** is the capital citadel — The Legion can never breach it.\n\n"
            "## Movement\n"
            "> • **Same cluster** → instant (free repositioning)\n"
            "> • **Adjacent cluster** → 1 turn *(must be at edge pos 1–6, not C)*\n"
            "> • **Different sector** → 2 turns via Citadel Hub A *(must be at corner X-N-N)*\n\n"
            "## Combat\n"
            "> Combat resolves automatically each turn. If your Number shares a position "
            "with a Legion unit, a d20 roll modified by your stats determines the outcome. "
            "Multiple Legion units in one position cause **battle fatigue** — each engagement "
            "drains your effective Attack and Morale.\n\n"
            "## Legion Unit Types\n"
            "> 🐺 **Grauwolf** — Balanced wolf-pack unit; the Legion's standard infantry\n"
            "> 🦁 **Löwe** — High Defence; deploy Vanguard Numbers to crack its armour\n"
            "> 🦕 **Dinosauria** — Swarm attacker; Fortress Numbers absorb it best\n"
            "> 🤖 **Juggernaut** — Heavy assault mech; demands high ATK to defeat\n"
            "> 👁️ **Shepherd** — Legion command unit with high Recon; Recon Numbers counter it\n\n"
            "## Stats\n"
            "> `ATK` — Damage output | `DEF` — Damage mitigation\n"
            "> `SPD` — Initiative (who strikes first) | `MOR` — Reroll chance on bad dice\n"
            "> `SUP` — Supply; drains 1 per turn outside the Citadel. Below 5 = -2 to all rolls. "
            "Scavenge in the field to recover.\n"
            "> `RCN` — Recon; two effects: (1) if your RCN exceeds the Legion unit's, "
            "you gain +1 ATK from battlefield intel. (2) Shepherd and Dinosauria gain +3 DEF against "
            "Numbers with RCN below 8 — high Recon Numbers negate this entirely.\n\n"
            "## Defeat & Fallback\n"
            "> If your Number loses a position, it falls back to the nearest friendly or neutral "
            "position. If the whole cluster is overrun it escalates to an adjacent cluster, then "
            "the outer sector. If all sectors B–G fall, **The Final Defense of the Citadel** "
            "is declared and all Numbers scatter inside Hub A for a last stand."
        ),
    )
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | Use 📋 My Number to check your position.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class SquadronCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(RegistrationView())
        bot.add_view(HQView())

    @app_commands.command(
        name="post_registration",
        description="[GM/Admin] Post the Handler enlistment embed to this channel.",
    )
    async def post_registration(self, interaction: discord.Interaction):
        # Defer immediately to avoid Discord's 3-second timeout
        await interaction.response.defer(ephemeral=False)

        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1",
                interaction.guild_id,
            )
            count = await conn.fetchval(
                "SELECT COUNT(DISTINCT owner_id) FROM squadrons WHERE guild_id=$1 AND is_active=TRUE",
                interaction.guild_id,
            )
            outer_rows = await conn.fetch(
                "SELECT status FROM hexes WHERE guild_id=$1 AND level=1 AND address != $2",
                interaction.guild_id, SAFE_HUB,
            )
        gm_role_id = config["gamemaster_role_id"] if config else None
        bot_owner_id = getattr(interaction.client, "bot_owner_id", 0)
        is_privileged = (
            (bot_owner_id and interaction.user.id == bot_owner_id)
            or interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator
            or (gm_role_id and any(r.id == gm_role_id for r in interaction.user.roles))
        )
        if not is_privileged:
            await interaction.followup.send("❌ GMs and Command only.", ephemeral=True)
            return

        all_legion = all(r["status"] in _LEGION_STATUSES for r in outer_rows) if outer_rows else False
        embed = _build_registration_embed(count or 0, all_legion)
        view = RegistrationView() if not all_legion else discord.ui.View()
        sent = await interaction.followup.send(embed=embed, view=view)

        _registration_messages[interaction.guild_id] = {
            "channel_id": interaction.channel_id,
            "message_id": sent.id,
        }

    @app_commands.command(
        name="post_hq",
        description="[GM/Admin] Post the Handler Command Terminal embed to this channel.",
    )
    async def post_hq(self, interaction: discord.Interaction):
        # Defer immediately to avoid Discord's 3-second timeout
        await interaction.response.defer(ephemeral=False)

        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1",
                interaction.guild_id,
            )
        gm_role_id = config["gamemaster_role_id"] if config else None
        bot_owner_id = getattr(interaction.client, "bot_owner_id", 0)
        is_privileged = (
            (bot_owner_id and interaction.user.id == bot_owner_id)
            or interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator
            or (gm_role_id and any(r.id == gm_role_id for r in interaction.user.roles))
        )
        if not is_privileged:
            await interaction.followup.send("❌ GMs and Command only.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🖥️ Command Terminal — Squadron 86",
            description=(
                "Welcome, Handler. Your Number is active on the front lines.\n\n"
                "> 📋 **My Number** — View your squadron's position and combat stats\n"
                "> 🚀 **Reposition** — Instructions for repositioning your Number\n"
                "> 🗺️ **Front Lines** — View the current strategic situation\n"
                "> 🎒 **Scavenge** — Attempt to recover supplies from the field\n"
                "> 📖 **Field Manual** — Rules, movement, combat, and Legion unit types"
            ),
            color=discord.Color.from_rgb(30, 60, 120),
        )
        embed.set_footer(text="Risk Universalis 3 — Squadron 86 | All responses are private.")
        await interaction.followup.send(embed=embed, view=HQView())

    @app_commands.command(name="squadron_move", description="Reposition your Number to a level-3 sector.")
    @app_commands.describe(
        squadron_name="Name of your Number squadron",
        address="Target level-3 position (e.g. B-2-4). Same cluster=instant. Adjacent cluster=1 turn. Different sector=2 turns via Citadel.",
    )
    async def move(self, interaction: discord.Interaction, squadron_name: str, address: str):
        address = address.strip().upper()
        await ensure_guild(interaction.guild_id)

        if level_of(address) != 3:
            await interaction.response.send_message(
                "❌ You must move to a level-3 position (e.g. `B-2-4` or `A-C-1`).", ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            sq = await conn.fetchrow(
                "SELECT id, name, hex_address, home_outer, in_transit, deploy_hex FROM squadrons "
                "WHERE guild_id=$1 AND owner_id=$2 AND name=$3 AND is_active=TRUE",
                interaction.guild_id, interaction.user.id, squadron_name,
            )
            if not sq:
                await interaction.response.send_message(
                    f"❌ No active Number named **{squadron_name}**.", ephemeral=True)
                return
            if sq["in_transit"]:
                await interaction.response.send_message(
                    "❌ This Number is already in transit. Await the next turn to resolve.",
                    ephemeral=True)
                return
            hex_exists = await conn.fetchrow(
                "SELECT address FROM hexes WHERE guild_id=$1 AND address=$2",
                interaction.guild_id, address,
            )
            if not hex_exists:
                await interaction.response.send_message(
                    f"❌ Position `{address}` doesn't exist.", ephemeral=True)
                return

            current = sq["hex_address"]
            current_mid = mid_of(current)
            current_outer = outer_of(current)
            target_mid = mid_of(address)
            target_outer = outer_of(address)

            if current_mid == target_mid:
                await conn.execute("UPDATE squadrons SET hex_address=$1 WHERE id=$2", address, sq["id"])
                await interaction.response.send_message(
                    f"📡 **{squadron_name}** repositioned to **{address}**.", ephemeral=True)
                return

            if target_outer == current_outer:
                if not is_edge_inner(current):
                    await interaction.response.send_message(
                        f"❌ **{squadron_name}** is at center position `C`. "
                        f"Move to an edge position (pos 1–6) first.", ephemeral=True)
                    return
                reachable = adjacent_inner_clusters(current)
                if target_mid not in reachable:
                    await interaction.response.send_message(
                        f"❌ Cannot reach `{target_mid}` from `{current}`. "
                        f"Reachable: {', '.join(reachable) or 'none'}.", ephemeral=True)
                    return
                entry = f"{target_mid}-C"
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2 WHERE id=$2",
                    entry, sq["id"])
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** crossing to cluster `{target_mid}`, arriving at `{entry}` next turn.",
                    ephemeral=True)
                return

            crossable_outer = can_cross_to_outer(current)
            if crossable_outer is None:
                await interaction.response.send_message(
                    f"❌ Cannot cross sectors from `{current}`. "
                    f"Move to a corner position (e.g. `B-2-2`) first.", ephemeral=True)
                return

            if current_outer == SAFE_HUB:
                entry = entry_hex_for_outer(target_outer, SAFE_HUB)
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2, home_outer=$2 WHERE id=$3",
                    entry, target_outer, sq["id"])
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** deploying to **Sector {target_outer}**, arriving at `{entry}` next turn.",
                    ephemeral=True)
            elif target_outer == SAFE_HUB:
                entry = entry_hex_for_outer(SAFE_HUB, current_outer)
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=2, home_outer=$2 WHERE id=$3",
                    entry, SAFE_HUB, sq["id"])
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** withdrawing to **Citadel Hub A**, arriving at `{entry}` next turn.",
                    ephemeral=True)
            else:
                hub_entry = entry_hex_for_outer(SAFE_HUB, current_outer)
                await conn.execute(
                    "UPDATE squadrons SET in_transit=TRUE, transit_destination=$1, transit_step=1, home_outer=$2 WHERE id=$3",
                    target_outer, SAFE_HUB, sq["id"])
                await interaction.response.send_message(
                    f"🚶 **{squadron_name}** begins transit **Sector {current_outer} → Citadel Hub A → Sector {target_outer}**. "
                    f"Arrives at `{hub_entry}` next turn (2 turns total).", ephemeral=True)

    @app_commands.command(name="squadron_status", description="View your Number's stats and position.")
    async def status(self, interaction: discord.Interaction):
        await _hq_status(interaction)


async def setup(bot):
    await bot.add_cog(SquadronCog(bot))
