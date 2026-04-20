"""
fob_cog.py — Forward Operating Base, Economy & Stock Market

Replaces the old "My Numbers" button with "My F.O.B." in HQ.

Economy loop:
  Raw Materials  ──scavenge/combat──► sell to Citadel ──► I.O.U.s
  I.O.U.s ──buy processed materials──► FOB upgrades
  I.O.U.s ──trade stocks──► more I.O.U.s (or losses)

FOB Buildings (all require Command Bunker tier >= building tier - 1):
  🏛️ Command Bunker  — gate building, unlocks higher tiers on others
  🏗️ Barracks        — +MOR / +SUP per tier
  🔧 Armory          — +ATK / +DEF per tier
  📡 Comms Tower     — +RCN / +SPD per tier
  🎒 Supply Depot    — raises supply cap and passive regen
  ⚙️ Workshop        — reduces transit steps at high tiers

Stock Market (GM-seeded, per-guild):
  Default tickers seeded on first use.
  Prices fluctuate each turn (called by turn engine hook).
  GMs can trigger market events via slash command.
"""

import random
import math
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import SAFE_HUB, outer_of

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Raw materials earned per successful scavenge (added ON TOP of existing supply gain)
RAW_MAT_SCAVENGE_SUCCESS = 3
# Raw materials earned for surviving/winning a combat turn (passive front-line reward)
RAW_MAT_COMBAT_WIN       = 2
RAW_MAT_COMBAT_SURVIVE   = 1  # even on a loss/draw

# How many raw materials = 1 I.O.U. when selling to Citadel
RAW_MAT_PER_IOU = 5

# Processed material shop defaults (cost in I.O.U.s → how many proc. mats you get)
DEFAULT_SHOP = {
    "processed_small":  {"label": "Small Processed Shipment",  "cost": 30,  "quantity": 5},
    "processed_medium": {"label": "Medium Processed Shipment", "cost": 75,  "quantity": 15},
    "processed_large":  {"label": "Large Processed Shipment",  "cost": 180, "quantity": 40},
}

# Default stocks seeded per guild
DEFAULT_STOCKS = [
    {"ticker": "MECH", "name": "Eighty-Six Mechanica Corp",   "price": 120, "trend": "bull"},
    {"ticker": "FUEL", "name": "Republic Fuel & Logistics",   "price": 80,  "trend": "stable"},
    {"ticker": "ARMS", "name": "Citadel Armaments Ltd",       "price": 150, "trend": "stable"},
    {"ticker": "RECON","name": "Horizon Recon Systems",       "price": 60,  "trend": "bear"},
    {"ticker": "SCRAP","name": "Reclaimed Materials Exchange", "price": 40,  "trend": "volatile"},
]

# ─────────────────────────────────────────────────────────────────────────────
# FOB building definitions
# ─────────────────────────────────────────────────────────────────────────────

BUILDINGS = {
    "command_bunker": {
        "label": "🏛️ Command Bunker",
        "short": "Bunker",
        "description": "The heart of your FOB. Gates upgrade tiers on all other buildings.",
        "tiers": [
            {"name": "Reinforced Shelter",  "proc_cost": 10,  "effect": "Unlocks Tier 1 on all buildings."},
            {"name": "Command Post",        "proc_cost": 25,  "effect": "Unlocks Tier 2 on all buildings. +1 I.O.U. per turn passive income."},
            {"name": "Forward HQ",          "proc_cost": 60,  "effect": "Unlocks Tier 3 on all buildings. +2 I.O.U. per turn."},
            {"name": "Tactical Operations", "proc_cost": 120, "effect": "Unlocks Tier 4 on all buildings. +3 I.O.U. per turn."},
            {"name": "Strategic Command",   "proc_cost": 250, "effect": "Unlocks Tier 5 on all buildings. +5 I.O.U. per turn."},
        ],
        "stat_bonus": {},  # no direct stat bonuses
    },
    "barracks": {
        "label": "🏗️ Barracks",
        "short": "Barracks",
        "description": "Houses and trains your Number. Improves morale and supply endurance.",
        "tiers": [
            {"name": "Makeshift Bunks",     "proc_cost": 8,   "effect": "+1 MOR, +1 SUP cap"},
            {"name": "Hardened Barracks",   "proc_cost": 20,  "effect": "+2 MOR, +2 SUP cap"},
            {"name": "Training Compound",   "proc_cost": 50,  "effect": "+3 MOR, +3 SUP cap, +1 SUP regen/turn"},
            {"name": "Elite Quarters",      "proc_cost": 100, "effect": "+4 MOR, +5 SUP cap, +2 SUP regen/turn"},
            {"name": "Legion-Grade Bivouac","proc_cost": 200, "effect": "+6 MOR, +8 SUP cap, +3 SUP regen/turn"},
        ],
        "stat_bonus": {1: {"morale": 1}, 2: {"morale": 2}, 3: {"morale": 3}, 4: {"morale": 4}, 5: {"morale": 6}},
    },
    "armory": {
        "label": "🔧 Armory",
        "short": "Armory",
        "description": "Weapons depot and maintenance bay. Directly improves your Number's attack and defence.",
        "tiers": [
            {"name": "Field Cache",         "proc_cost": 10,  "effect": "+1 ATK, +1 DEF"},
            {"name": "Munitions Store",     "proc_cost": 25,  "effect": "+2 ATK, +2 DEF"},
            {"name": "Weapons Workshop",    "proc_cost": 60,  "effect": "+3 ATK, +3 DEF"},
            {"name": "Heavy Armament Bay",  "proc_cost": 120, "effect": "+5 ATK, +4 DEF"},
            {"name": "Juggernaut Forge",    "proc_cost": 240, "effect": "+7 ATK, +6 DEF"},
        ],
        "stat_bonus": {1: {"attack": 1, "defense": 1}, 2: {"attack": 2, "defense": 2},
                       3: {"attack": 3, "defense": 3}, 4: {"attack": 5, "defense": 4},
                       5: {"attack": 7, "defense": 6}},
    },
    "comms_tower": {
        "label": "📡 Comms Tower",
        "short": "Comms",
        "description": "Battlefield intelligence and coordination array. Boosts recon and speed.",
        "tiers": [
            {"name": "Field Radio Post",    "proc_cost": 8,   "effect": "+1 RCN, +1 SPD"},
            {"name": "Signal Array",        "proc_cost": 20,  "effect": "+2 RCN, +2 SPD"},
            {"name": "Encrypted Net",       "proc_cost": 50,  "effect": "+3 RCN, +3 SPD"},
            {"name": "Deep Scan Relay",     "proc_cost": 100, "effect": "+5 RCN, +4 SPD"},
            {"name": "Shepherd-Class Net",  "proc_cost": 200, "effect": "+7 RCN, +6 SPD"},
        ],
        "stat_bonus": {1: {"recon": 1, "speed": 1}, 2: {"recon": 2, "speed": 2},
                       3: {"recon": 3, "speed": 3}, 4: {"recon": 5, "speed": 4},
                       5: {"recon": 7, "speed": 6}},
    },
    "supply_depot": {
        "label": "🎒 Supply Depot",
        "short": "Depot",
        "description": "Logistics hub that extends supply range and passively restores resources.",
        "tiers": [
            {"name": "Crate Store",         "proc_cost": 8,   "effect": "Supply cap +5 (→25). +0.5 passive regen/turn."},
            {"name": "Logistics Post",      "proc_cost": 20,  "effect": "Supply cap +10 (→30). +1 regen/turn."},
            {"name": "Forward Supply Hub",  "proc_cost": 50,  "effect": "Supply cap +15 (→35). +1 regen/turn outside Citadel."},
            {"name": "Regional Depot",      "proc_cost": 100, "effect": "Supply cap +20 (→40). +2 regen/turn."},
            {"name": "Citadel Lifeline",    "proc_cost": 200, "effect": "Supply cap +30 (→50). +3 regen/turn. Drain halved."},
        ],
        # Supply cap/regen handled specially in turn engine — not a flat stat bonus
        "stat_bonus": {},
    },
    "workshop": {
        "label": "⚙️ Workshop",
        "short": "Workshop",
        "description": "Maintenance and logistics facility. Reduces transit steps at high tiers.",
        "tiers": [
            {"name": "Repair Tent",         "proc_cost": 8,   "effect": "No transit benefit yet. Allows FOB building repairs."},
            {"name": "Mobile Workshop",     "proc_cost": 20,  "effect": "Adjacent-cluster transit time -0 (preparation for Tier 3)."},
            {"name": "Rapid Transit Post",  "proc_cost": 50,  "effect": "Adjacent-cluster moves become instant (0 turns)."},
            {"name": "Logistics Network",   "proc_cost": 100, "effect": "Cross-sector transit reduced to 1 turn (from 2)."},
            {"name": "Legion-Speed Rail",   "proc_cost": 200, "effect": "All transit instant. Scavenge cooldown reset on sector move."},
        ],
        "stat_bonus": {},
    },
}

BUILDING_ORDER = ["command_bunker", "barracks", "armory", "comms_tower", "supply_depot", "workshop"]

# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

async def ensure_player_economy(conn, guild_id: int, owner_id: int):
    await conn.execute(
        "INSERT INTO player_economy (guild_id, owner_id, raw_materials, ious) "
        "VALUES ($1,$2,0,0) ON CONFLICT (guild_id, owner_id) DO NOTHING",
        guild_id, owner_id,
    )

async def get_economy(conn, guild_id: int, owner_id: int) -> dict:
    await ensure_player_economy(conn, guild_id, owner_id)
    row = await conn.fetchrow(
        "SELECT raw_materials, ious FROM player_economy WHERE guild_id=$1 AND owner_id=$2",
        guild_id, owner_id,
    )
    return dict(row)

async def get_fob_tiers(conn, guild_id: int, owner_id: int) -> dict:
    rows = await conn.fetch(
        "SELECT building, tier FROM fob_buildings WHERE guild_id=$1 AND owner_id=$2",
        guild_id, owner_id,
    )
    tiers = {b: 0 for b in BUILDINGS}
    for r in rows:
        if r["building"] in tiers:
            tiers[r["building"]] = r["tier"]
    return tiers


async def get_fob_stat_bonuses(conn, guild_id: int, owner_id: int) -> dict:
    """Return total stat bonuses from all FOB buildings for this player."""
    tiers = await get_fob_tiers(conn, guild_id, owner_id)
    totals = {"attack": 0, "defense": 0, "speed": 0, "morale": 0, "recon": 0}
    for bkey, tier in tiers.items():
        if tier == 0:
            continue
        bonuses = BUILDINGS[bkey].get("stat_bonus", {}).get(tier, {})
        for stat, val in bonuses.items():
            if stat in totals:
                totals[stat] += val
    return totals


async def get_supply_depot_bonus(conn, guild_id: int, owner_id: int) -> dict:
    """Returns supply cap bonus and regen bonus from Supply Depot."""
    tiers = await get_fob_tiers(conn, guild_id, owner_id)
    depot = tiers.get("supply_depot", 0)
    cap_bonus   = [0, 5, 10, 15, 20, 30][depot]
    regen_bonus = [0, 0, 1,  1,  2,  3][depot]
    drain_half  = depot >= 5
    return {"cap_bonus": cap_bonus, "regen_bonus": regen_bonus, "drain_half": drain_half}


async def get_workshop_tier(conn, guild_id: int, owner_id: int) -> int:
    tiers = await get_fob_tiers(conn, guild_id, owner_id)
    return tiers.get("workshop", 0)


async def seed_stocks_if_needed(conn, guild_id: int):
    count = await conn.fetchval("SELECT COUNT(*) FROM stocks WHERE guild_id=$1", guild_id)
    if count == 0:
        for s in DEFAULT_STOCKS:
            await conn.execute(
                "INSERT INTO stocks (guild_id, ticker, name, price, trend) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                guild_id, s["ticker"], s["name"], s["price"], s["trend"],
            )


async def seed_shop_if_needed(conn, guild_id: int):
    count = await conn.fetchval("SELECT COUNT(*) FROM citadel_shop WHERE guild_id=$1", guild_id)
    if count == 0:
        for key, data in DEFAULT_SHOP.items():
            await conn.execute(
                "INSERT INTO citadel_shop (guild_id, item, cost_ious, quantity) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                guild_id, key, data["cost"], data["quantity"],
            )


# ─────────────────────────────────────────────────────────────────────────────
# Economy: raw material rewards (called externally by turn engine)
# ─────────────────────────────────────────────────────────────────────────────

async def award_combat_raw_materials(conn, guild_id: int, owner_id: int, won: bool):
    """Called by turn engine when a player's squadron survives or wins combat."""
    await ensure_player_economy(conn, guild_id, owner_id)
    amount = RAW_MAT_COMBAT_WIN if won else RAW_MAT_COMBAT_SURVIVE
    await conn.execute(
        "UPDATE player_economy SET raw_materials = raw_materials + $1 "
        "WHERE guild_id=$2 AND owner_id=$3",
        amount, guild_id, owner_id,
    )


async def award_scavenge_raw_materials(conn, guild_id: int, owner_id: int):
    """Called by scavenge handler on a successful roll."""
    await ensure_player_economy(conn, guild_id, owner_id)
    await conn.execute(
        "UPDATE player_economy SET raw_materials = raw_materials + $1 "
        "WHERE guild_id=$2 AND owner_id=$3",
        RAW_MAT_SCAVENGE_SUCCESS, guild_id, owner_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stock market fluctuation (called by turn engine each turn)
# ─────────────────────────────────────────────────────────────────────────────

async def fluctuate_stocks(conn, guild_id: int):
    """Apply per-turn random price movement based on trend."""
    rows = await conn.fetch("SELECT ticker, price, trend FROM stocks WHERE guild_id=$1", guild_id)
    for row in rows:
        price = row["price"]
        trend = row["trend"]
        if trend == "bull":
            delta = random.randint(0, 12) - 3        # +9 to -3 bias up
        elif trend == "bear":
            delta = random.randint(-12, 3)            # -9 to +3 bias down
        elif trend == "volatile":
            delta = random.randint(-20, 20)           # wild swing
        else:  # stable
            delta = random.randint(-5, 5)
        new_price = max(5, price + delta)
        # Chance to shift trend
        if random.random() < 0.08:
            new_trend = random.choice(["bull", "bear", "stable", "volatile"])
        else:
            new_trend = trend
        await conn.execute(
            "UPDATE stocks SET price=$1, trend=$2, last_updated=NOW() "
            "WHERE guild_id=$3 AND ticker=$4",
            new_price, new_trend, guild_id, row["ticker"],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Command Bunker passive I.O.U. income (called by turn engine each turn)
# ─────────────────────────────────────────────────────────────────────────────

async def apply_bunker_income(conn, guild_id: int):
    """Give I.O.U. passive income to all players with a Command Bunker."""
    bunker_income = {1: 0, 2: 1, 3: 2, 4: 3, 5: 5}
    rows = await conn.fetch(
        "SELECT owner_id, tier FROM fob_buildings "
        "WHERE guild_id=$1 AND building='command_bunker' AND tier > 0",
        guild_id,
    )
    for row in rows:
        income = bunker_income.get(row["tier"], 0)
        if income > 0:
            await ensure_player_economy(conn, guild_id, row["owner_id"])
            await conn.execute(
                "UPDATE player_economy SET ious = ious + $1 WHERE guild_id=$2 AND owner_id=$3",
                income, guild_id, row["owner_id"],
            )


# ─────────────────────────────────────────────────────────────────────────────
# FOB View — main menu
# ─────────────────────────────────────────────────────────────────────────────

class FOBView(discord.ui.View):
    def __init__(self, guild_id: int, owner_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id

    @discord.ui.button(label="🏛️ Buildings", style=discord.ButtonStyle.primary, row=0)
    async def buildings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This isn't your FOB.", ephemeral=True)
            return
        await _show_fob_buildings(interaction, self.guild_id, self.owner_id)

    @discord.ui.button(label="🏪 Citadel Shop", style=discord.ButtonStyle.secondary, row=0)
    async def shop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This isn't your FOB.", ephemeral=True)
            return
        await _show_citadel_shop(interaction, self.guild_id, self.owner_id)

    @discord.ui.button(label="💹 Stock Market", style=discord.ButtonStyle.secondary, row=0)
    async def market_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This isn't your FOB.", ephemeral=True)
            return
        await _show_stock_market(interaction, self.guild_id, self.owner_id)

    @discord.ui.button(label="📦 Sell Raw Materials", style=discord.ButtonStyle.success, row=1)
    async def sell_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This isn't your FOB.", ephemeral=True)
            return
        await _sell_raw_materials(interaction, self.guild_id, self.owner_id)

    @discord.ui.button(label="💰 My Wallet", style=discord.ButtonStyle.gray, row=1)
    async def wallet_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This isn't your FOB.", ephemeral=True)
            return
        await _show_wallet(interaction, self.guild_id, self.owner_id)


# ─────────────────────────────────────────────────────────────────────────────
# FOB sub-screens
# ─────────────────────────────────────────────────────────────────────────────

async def _show_fob_overview(interaction: discord.Interaction, guild_id: int, owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        econ  = await get_economy(conn, guild_id, owner_id)
        tiers = await get_fob_tiers(conn, guild_id, owner_id)

    bunker = tiers["command_bunker"]
    bunker_income = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 5}

    lines = []
    for bkey in BUILDING_ORDER:
        bdata = BUILDINGS[bkey]
        tier  = tiers[bkey]
        if tier == 0:
            tier_str = "*(not built)*"
        else:
            tier_str = f"Tier {tier} — **{bdata['tiers'][tier-1]['name']}**"
        lines.append(f"{bdata['label']}: {tier_str}")

    embed = discord.Embed(
        title=f"🪖 {interaction.user.display_name}'s Forward Operating Base",
        description=(
            f"**💰 I.O.U.s:** `{econ['ious']}` | **📦 Raw Materials:** `{econ['raw_materials']}`\n"
            f"**Bunker Passive Income:** +{bunker_income[bunker]} I.O.U./turn\n\n"
            "**── Installed Buildings ──**\n"
            + "\n".join(lines)
            + "\n\n*Use the buttons below to upgrade, trade, or buy supplies.*"
        ),
        color=discord.Color.from_rgb(60, 100, 60),
    )
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | Your FOB is your lifeline.")
    view = FOBView(guild_id=guild_id, owner_id=owner_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _show_fob_buildings(interaction: discord.Interaction, guild_id: int, owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        econ  = await get_economy(conn, guild_id, owner_id)
        tiers = await get_fob_tiers(conn, guild_id, owner_id)

    bunker_tier = tiers["command_bunker"]
    embed = discord.Embed(
        title="🏛️ FOB Buildings",
        description=(
            f"**💰 I.O.U.s:** `{econ['ious']}` | **📦 Raw Materials:** `{econ['raw_materials']}`\n"
            f"**Command Bunker Tier:** {bunker_tier}/5 — unlocks up to Tier {bunker_tier} on all buildings.\n\n"
            "*Select a building to upgrade. Upgrades cost **Processed Materials** (buy from Citadel Shop).*\n"
            "*Upgrade the **Command Bunker** first to unlock higher tiers on other buildings.*"
        ),
        color=discord.Color.from_rgb(80, 80, 120),
    )
    for bkey in BUILDING_ORDER:
        bdata = BUILDINGS[bkey]
        tier  = tiers[bkey]
        max_tier = 5
        if tier >= max_tier:
            status = "✅ **MAXED**"
        elif tier == 0:
            next_tier_data = bdata["tiers"][0]
            locked = (bkey != "command_bunker" and bunker_tier < 1)
            status = f"{'🔒 Locked' if locked else '⬜ Not built'} — Next: {next_tier_data['name']} (`{next_tier_data['proc_cost']}` proc. mats)"
        else:
            current_name = bdata["tiers"][tier - 1]["name"]
            next_tier_data = bdata["tiers"][tier]
            locked = (bkey != "command_bunker" and bunker_tier < tier + 1)
            if locked:
                next_label = "🔒 Locked (upgrade Bunker first)"
            else:
                next_name = bdata["tiers"][tier]["name"]
                next_cost = next_tier_data["proc_cost"]
                next_label = f"Next: {next_name} (`{next_cost}` proc. mats)"
            status = f"Tier {tier} — **{current_name}**\n> {next_label}"
        embed.add_field(
            name=bdata["label"],
            value=f"{status}\n> *{bdata['description']}*",
            inline=False,
        )
    view = BuildingUpgradeView(guild_id=guild_id, owner_id=owner_id, tiers=tiers)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _show_citadel_shop(interaction: discord.Interaction, guild_id: int, owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await seed_shop_if_needed(conn, guild_id)
        econ  = await get_economy(conn, guild_id, owner_id)
        items = await conn.fetch(
            "SELECT item, cost_ious, quantity FROM citadel_shop WHERE guild_id=$1 ORDER BY cost_ious",
            guild_id,
        )

    embed = discord.Embed(
        title="🏪 Citadel Supply Exchange",
        description=(
            f"**💰 Your I.O.U.s:** `{econ['ious']}`\n\n"
            "Trade your **I.O.U.s** for **Processed Materials** to upgrade your FOB.\n"
            "First, sell Raw Materials from the **My F.O.B.** menu to earn I.O.U.s.\n\n"
            f"*Conversion rate: {RAW_MAT_PER_IOU} Raw Materials = 1 I.O.U.*"
        ),
        color=discord.Color.from_rgb(180, 140, 40),
    )
    for item in items:
        label = DEFAULT_SHOP.get(item["item"], {}).get("label", item["item"])
        embed.add_field(
            name=f"📦 {label}",
            value=f"**Cost:** `{item['cost_ious']}` I.O.U.s → `{item['quantity']}` Processed Materials",
            inline=False,
        )
    view = CitadelShopView(guild_id=guild_id, owner_id=owner_id, items=list(items), ious=econ["ious"])
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _show_stock_market(interaction: discord.Interaction, guild_id: int, owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await seed_stocks_if_needed(conn, guild_id)
        econ   = await get_economy(conn, guild_id, owner_id)
        stocks = await conn.fetch(
            "SELECT ticker, name, price, trend FROM stocks WHERE guild_id=$1 ORDER BY ticker",
            guild_id,
        )
        holdings = await conn.fetch(
            "SELECT ticker, shares FROM stock_holdings WHERE guild_id=$1 AND owner_id=$2",
            guild_id, owner_id,
        )

    holding_map = {h["ticker"]: h["shares"] for h in holdings}
    trend_icon = {"bull": "📈", "bear": "📉", "stable": "➡️", "volatile": "⚡"}

    embed = discord.Embed(
        title="💹 Republic Stock Exchange",
        description=(
            f"**💰 Your I.O.U.s:** `{econ['ious']}`\n"
            "Invest I.O.U.s in stocks. Prices fluctuate every Legion advance (turn).\n"
            "GMs can trigger market events to move trends.\n\n"
        ),
        color=discord.Color.from_rgb(40, 120, 80),
    )
    for s in stocks:
        icon = trend_icon.get(s["trend"], "➡️")
        owned = holding_map.get(s["ticker"], 0)
        owned_str = f" *(you own: {owned} shares)*" if owned > 0 else ""
        embed.add_field(
            name=f"{icon} [{s['ticker']}] {s['name']}",
            value=f"**Price:** `{s['price']} I.O.U.s/share` | Trend: {s['trend'].title()}{owned_str}",
            inline=False,
        )
    view = StockMarketView(guild_id=guild_id, owner_id=owner_id, stocks=list(stocks), ious=econ["ious"])
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _sell_raw_materials(interaction: discord.Interaction, guild_id: int, owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        econ = await get_economy(conn, guild_id, owner_id)
        raw  = econ["raw_materials"]
        if raw < RAW_MAT_PER_IOU:
            await interaction.response.send_message(
                f"❌ You need at least **{RAW_MAT_PER_IOU} Raw Materials** to sell.\n"
                f"You have: `{raw}`. Scavenge or survive battles to earn more.",
                ephemeral=True,
            )
            return
        earned    = raw // RAW_MAT_PER_IOU
        remainder = raw % RAW_MAT_PER_IOU
        await conn.execute(
            "UPDATE player_economy SET raw_materials=$1, ious=ious+$2 WHERE guild_id=$3 AND owner_id=$4",
            remainder, earned, guild_id, owner_id,
        )
        new_econ = await get_economy(conn, guild_id, owner_id)

    embed = discord.Embed(
        title="📦 Raw Materials Sold to Citadel",
        description=(
            f"Converted `{raw - remainder}` Raw Materials → **+{earned} I.O.U.s**\n"
            f"> Remainder kept: `{remainder}` Raw Materials\n\n"
            f"**New balance:** `{new_econ['ious']}` I.O.U.s | `{new_econ['raw_materials']}` Raw Materials"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _show_wallet(interaction: discord.Interaction, guild_id: int, owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        econ     = await get_economy(conn, guild_id, owner_id)
        tiers    = await get_fob_tiers(conn, guild_id, owner_id)
        bonuses  = await get_fob_stat_bonuses(conn, guild_id, owner_id)
        depot    = await get_supply_depot_bonus(conn, guild_id, owner_id)
        holdings = await conn.fetch(
            "SELECT sh.ticker, sh.shares, s.price FROM stock_holdings sh "
            "JOIN stocks s ON s.guild_id=sh.guild_id AND s.ticker=sh.ticker "
            "WHERE sh.guild_id=$1 AND sh.owner_id=$2 AND sh.shares > 0",
            guild_id, owner_id,
        )

    portfolio_val = sum(h["shares"] * h["price"] for h in holdings)
    bunker_tier   = tiers["command_bunker"]
    bunker_income = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 5}

    stat_lines = " | ".join(
        f"+{v} {k[:3].upper()}" for k, v in bonuses.items() if v > 0
    ) or "None yet"

    embed = discord.Embed(
        title=f"💰 {interaction.user.display_name}'s Wallet & FOB Summary",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="💵 Currency",
        value=(
            f"I.O.U.s: `{econ['ious']}`\n"
            f"Raw Materials: `{econ['raw_materials']}`\n"
            f"Portfolio Value: ~`{portfolio_val}` I.O.U.s\n"
            f"Bunker Passive: +`{bunker_income[bunker_tier]}` I.O.U./turn"
        ),
        inline=True,
    )
    embed.add_field(
        name="⚔️ Active FOB Stat Bonuses",
        value=stat_lines,
        inline=True,
    )
    embed.add_field(
        name="🎒 Supply Depot",
        value=(
            f"Cap +{depot['cap_bonus']} | Regen +{depot['regen_bonus']}/turn"
            + (" | Drain halved ✅" if depot["drain_half"] else "")
        ),
        inline=True,
    )
    if holdings:
        hold_lines = "\n".join(
            f"`{h['ticker']}`: {h['shares']} shares @ {h['price']} = {h['shares']*h['price']} I.O.U.s"
            for h in holdings
        )
        embed.add_field(name="📈 Stock Holdings", value=hold_lines, inline=False)
    embed.set_footer(text="Risk Universalis 3 — Squadron 86 | FOB keeps your Number in the fight.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Building upgrade view
# ─────────────────────────────────────────────────────────────────────────────

class BuildingUpgradeView(discord.ui.View):
    def __init__(self, guild_id: int, owner_id: int, tiers: dict):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        bunker = tiers["command_bunker"]
        for i, bkey in enumerate(BUILDING_ORDER):
            tier = tiers[bkey]
            can_upgrade = tier < 5
            is_locked   = (bkey != "command_bunker" and bunker < tier + 1)
            bdata = BUILDINGS[bkey]
            label = f"{bdata['label']} (T{tier}→T{tier+1})" if can_upgrade else f"{bdata['label']} ✅"
            style = discord.ButtonStyle.success if (can_upgrade and not is_locked) else discord.ButtonStyle.gray
            btn = discord.ui.Button(
                label=label, style=style, row=i // 3,
                custom_id=f"fob_upgrade_{bkey}",
                disabled=(not can_upgrade or is_locked),
            )
            btn.callback = self._make_callback(bkey)
            self.add_item(btn)

    def _make_callback(self, bkey: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ Not your FOB.", ephemeral=True)
                return
            await _do_upgrade(interaction, self.guild_id, self.owner_id, bkey)
        return callback


async def _do_upgrade(interaction: discord.Interaction, guild_id: int, owner_id: int, bkey: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        tiers  = await get_fob_tiers(conn, guild_id, owner_id)
        cur    = tiers[bkey]
        bunker = tiers["command_bunker"]

        if cur >= 5:
            await interaction.response.send_message("✅ Already maxed.", ephemeral=True)
            return
        if bkey != "command_bunker" and bunker < cur + 1:
            await interaction.response.send_message(
                f"🔒 Upgrade your **Command Bunker** to Tier {cur + 1} first.", ephemeral=True)
            return

        bdata     = BUILDINGS[bkey]
        next_tier = cur + 1
        cost_proc = bdata["tiers"][cur]["proc_cost"]

        # Processed materials are stored as ious spent — player must have them
        # We use a separate "processed_materials" virtual currency:
        # For simplicity: processed materials are tracked as a column in player_economy
        econ = await conn.fetchrow(
            "SELECT processed_materials FROM player_economy WHERE guild_id=$1 AND owner_id=$2",
            guild_id, owner_id,
        )
        # Handle column not existing yet gracefully
        proc_mats = econ["processed_materials"] if econ and "processed_materials" in econ.keys() else 0

        if proc_mats < cost_proc:
            await interaction.response.send_message(
                f"❌ Not enough **Processed Materials**.\n"
                f"Need `{cost_proc}`, you have `{proc_mats}`.\n"
                f"Buy them from the **🏪 Citadel Shop** using I.O.U.s.",
                ephemeral=True,
            )
            return

        # Deduct and apply upgrade
        await conn.execute(
            "UPDATE player_economy SET processed_materials = processed_materials - $1 "
            "WHERE guild_id=$2 AND owner_id=$3",
            cost_proc, guild_id, owner_id,
        )
        await conn.execute(
            "INSERT INTO fob_buildings (guild_id, owner_id, building, tier) "
            "VALUES ($1,$2,$3,$4) ON CONFLICT (guild_id, owner_id, building) "
            "DO UPDATE SET tier=$4",
            guild_id, owner_id, bkey, next_tier,
        )

    tier_data = bdata["tiers"][cur]
    embed = discord.Embed(
        title=f"✅ {bdata['label']} Upgraded!",
        description=(
            f"**{bdata['short']}** is now **Tier {next_tier}** — *{tier_data['name']}*\n\n"
            f"> {tier_data['effect']}\n\n"
            f"Cost: `{cost_proc}` Processed Materials"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Citadel Shop View
# ─────────────────────────────────────────────────────────────────────────────

class CitadelShopView(discord.ui.View):
    def __init__(self, guild_id, owner_id, items, ious):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        for i, item in enumerate(items[:4]):
            label_key = item["item"]
            label = DEFAULT_SHOP.get(label_key, {}).get("label", label_key)
            short = label.split(" ")[0]  # first word
            btn = discord.ui.Button(
                label=f"Buy {short} ({item['cost_ious']} I.O.U.s)",
                style=discord.ButtonStyle.success if ious >= item["cost_ious"] else discord.ButtonStyle.gray,
                row=i // 2,
                disabled=ious < item["cost_ious"],
            )
            btn.callback = self._make_callback(label_key, item["cost_ious"], item["quantity"])
            self.add_item(btn)

    def _make_callback(self, item_key, cost, qty):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ Not your shop.", ephemeral=True)
                return
            await _buy_from_shop(interaction, self.guild_id, self.owner_id, item_key, cost, qty)
        return callback


async def _buy_from_shop(interaction, guild_id, owner_id, item_key, cost, qty):
    pool = await get_pool()
    async with pool.acquire() as conn:
        econ = await get_economy(conn, guild_id, owner_id)
        if econ["ious"] < cost:
            await interaction.response.send_message(
                f"❌ Not enough I.O.U.s. Need `{cost}`, have `{econ['ious']}`.", ephemeral=True)
            return
        await conn.execute(
            "UPDATE player_economy SET ious = ious - $1, processed_materials = processed_materials + $2 "
            "WHERE guild_id=$3 AND owner_id=$4",
            cost, qty, guild_id, owner_id,
        )
        new_econ = await conn.fetchrow(
            "SELECT ious, processed_materials FROM player_economy WHERE guild_id=$1 AND owner_id=$2",
            guild_id, owner_id,
        )
    label = DEFAULT_SHOP.get(item_key, {}).get("label", item_key)
    embed = discord.Embed(
        title="🏪 Purchase Confirmed",
        description=(
            f"Bought **{label}** from the Citadel.\n"
            f"> +`{qty}` Processed Materials\n"
            f"> -`{cost}` I.O.U.s\n\n"
            f"**Balance:** `{new_econ['ious']}` I.O.U.s | `{new_econ['processed_materials']}` Proc. Mats"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Stock Market View
# ─────────────────────────────────────────────────────────────────────────────

class StockMarketView(discord.ui.View):
    def __init__(self, guild_id, owner_id, stocks, ious):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.stocks   = stocks
        self.ious     = ious
        # Buy/Sell buttons per stock
        for i, s in enumerate(stocks[:4]):
            buy_btn = discord.ui.Button(
                label=f"Buy {s['ticker']}", style=discord.ButtonStyle.success,
                row=i // 2, custom_id=f"stock_buy_{s['ticker']}",
                disabled=ious < s["price"],
            )
            sell_btn = discord.ui.Button(
                label=f"Sell {s['ticker']}", style=discord.ButtonStyle.danger,
                row=i // 2, custom_id=f"stock_sell_{s['ticker']}",
            )
            buy_btn.callback  = self._buy_cb(s["ticker"], s["price"])
            sell_btn.callback = self._sell_cb(s["ticker"], s["price"])
            self.add_item(buy_btn)
            self.add_item(sell_btn)

    def _buy_cb(self, ticker, price):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ Not yours.", ephemeral=True)
                return
            await _buy_stock(interaction, self.guild_id, self.owner_id, ticker, price, qty=1)
        return callback

    def _sell_cb(self, ticker, price):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("❌ Not yours.", ephemeral=True)
                return
            await _sell_stock(interaction, self.guild_id, self.owner_id, ticker, price, qty=1)
        return callback


async def _buy_stock(interaction, guild_id, owner_id, ticker, price, qty):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Re-fetch live price
        row = await conn.fetchrow("SELECT price FROM stocks WHERE guild_id=$1 AND ticker=$2", guild_id, ticker)
        if not row:
            await interaction.response.send_message("❌ Stock not found.", ephemeral=True)
            return
        live_price = row["price"]
        total_cost = live_price * qty
        econ = await get_economy(conn, guild_id, owner_id)
        if econ["ious"] < total_cost:
            await interaction.response.send_message(
                f"❌ Not enough I.O.U.s. Need `{total_cost}`, have `{econ['ious']}`.", ephemeral=True)
            return
        await conn.execute(
            "UPDATE player_economy SET ious = ious - $1 WHERE guild_id=$2 AND owner_id=$3",
            total_cost, guild_id, owner_id,
        )
        await conn.execute(
            "INSERT INTO stock_holdings (guild_id, owner_id, ticker, shares) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (guild_id, owner_id, ticker) DO UPDATE SET shares = stock_holdings.shares + $4",
            guild_id, owner_id, ticker, qty,
        )
        new_econ = await get_economy(conn, guild_id, owner_id)
        shares = await conn.fetchval(
            "SELECT shares FROM stock_holdings WHERE guild_id=$1 AND owner_id=$2 AND ticker=$3",
            guild_id, owner_id, ticker,
        )
    embed = discord.Embed(
        title=f"📈 Bought {qty}x [{ticker}]",
        description=(
            f"Purchased `{qty}` share(s) of **{ticker}** at `{live_price}` I.O.U.s each.\n"
            f"> Total spent: `{total_cost}` I.O.U.s\n"
            f"> You now hold: `{shares}` shares\n\n"
            f"**Wallet:** `{new_econ['ious']}` I.O.U.s"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _sell_stock(interaction, guild_id, owner_id, ticker, price, qty):
    pool = await get_pool()
    async with pool.acquire() as conn:
        holding = await conn.fetchrow(
            "SELECT shares FROM stock_holdings WHERE guild_id=$1 AND owner_id=$2 AND ticker=$3",
            guild_id, owner_id, ticker,
        )
        if not holding or holding["shares"] < qty:
            await interaction.response.send_message(
                f"❌ You don't have enough `{ticker}` shares to sell.", ephemeral=True)
            return
        row = await conn.fetchrow("SELECT price FROM stocks WHERE guild_id=$1 AND ticker=$2", guild_id, ticker)
        live_price = row["price"]
        total_gain = live_price * qty
        await conn.execute(
            "UPDATE stock_holdings SET shares = shares - $1 "
            "WHERE guild_id=$2 AND owner_id=$3 AND ticker=$4",
            qty, guild_id, owner_id, ticker,
        )
        await conn.execute(
            "UPDATE player_economy SET ious = ious + $1 WHERE guild_id=$2 AND owner_id=$3",
            total_gain, guild_id, owner_id,
        )
        new_econ = await get_economy(conn, guild_id, owner_id)
        new_shares = await conn.fetchval(
            "SELECT shares FROM stock_holdings WHERE guild_id=$1 AND owner_id=$2 AND ticker=$3",
            guild_id, owner_id, ticker,
        ) or 0
    embed = discord.Embed(
        title=f"📉 Sold {qty}x [{ticker}]",
        description=(
            f"Sold `{qty}` share(s) of **{ticker}** at `{live_price}` I.O.U.s each.\n"
            f"> Total received: `+{total_gain}` I.O.U.s\n"
            f"> Shares remaining: `{new_shares}`\n\n"
            f"**Wallet:** `{new_econ['ious']}` I.O.U.s"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class FOBCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="fob", description="View and manage your Forward Operating Base.")
    async def fob_cmd(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            sq = await conn.fetchrow(
                "SELECT id FROM squadrons WHERE guild_id=$1 AND owner_id=$2 AND is_active=TRUE LIMIT 1",
                interaction.guild_id, interaction.user.id,
            )
        if not sq:
            await interaction.response.send_message(
                "❌ You haven't enlisted yet. Register via the **Handler Enlistment** embed first.",
                ephemeral=True,
            )
            return
        await _show_fob_overview(interaction, interaction.guild_id, interaction.user.id)

    # ── GM Commands ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="market_event",
        description="[GM] Trigger a market event affecting stock trends.",
    )
    @app_commands.describe(
        event="Preset market event to trigger",
    )
    @app_commands.choices(event=[
        app_commands.Choice(name="Legion Offensive (ARMS↑, FUEL↑, MECH↓)", value="legion_offensive"),
        app_commands.Choice(name="Ceasefire (MECH↑, SCRAP↓, RECON↑)",      value="ceasefire"),
        app_commands.Choice(name="Supply Shortage (FUEL↑↑, SCRAP↑, ARMS↓)", value="supply_shortage"),
        app_commands.Choice(name="Black Market (all volatile)",              value="black_market"),
        app_commands.Choice(name="Citadel Boom (all bull)",                  value="citadel_boom"),
        app_commands.Choice(name="Market Crash (all bear)",                  value="market_crash"),
    ])
    async def market_event(self, interaction: discord.Interaction, event: str):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            cfg = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1", interaction.guild_id
            )
        gm_role_id    = cfg["gamemaster_role_id"] if cfg else None
        bot_owner_id  = getattr(interaction.client, "bot_owner_id", 0)
        is_privileged = (
            (bot_owner_id and interaction.user.id == bot_owner_id)
            or interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator
            or (gm_role_id and any(r.id == gm_role_id for r in interaction.user.roles))
        )
        if not is_privileged:
            await interaction.response.send_message("❌ GMs only.", ephemeral=True)
            return

        EVENTS = {
            "legion_offensive": {"ARMS": "bull", "FUEL": "bull", "MECH": "bear", "RECON": "stable", "SCRAP": "stable"},
            "ceasefire":        {"MECH": "bull", "SCRAP": "bear", "RECON": "bull", "ARMS": "stable", "FUEL": "stable"},
            "supply_shortage":  {"FUEL": "volatile", "SCRAP": "bull", "ARMS": "bear", "MECH": "stable", "RECON": "stable"},
            "black_market":     {t: "volatile" for t in ["MECH","FUEL","ARMS","RECON","SCRAP"]},
            "citadel_boom":     {t: "bull"     for t in ["MECH","FUEL","ARMS","RECON","SCRAP"]},
            "market_crash":     {t: "bear"     for t in ["MECH","FUEL","ARMS","RECON","SCRAP"]},
        }
        trend_map = EVENTS[event]
        async with pool.acquire() as conn:
            await seed_stocks_if_needed(conn, interaction.guild_id)
            for ticker, trend in trend_map.items():
                await conn.execute(
                    "UPDATE stocks SET trend=$1 WHERE guild_id=$2 AND ticker=$3",
                    trend, interaction.guild_id, ticker,
                )

        event_names = {
            "legion_offensive": "⚔️ Legion Offensive",
            "ceasefire":        "🕊️ Ceasefire Declaration",
            "supply_shortage":  "📦 Supply Shortage",
            "black_market":     "🌑 Black Market Surge",
            "citadel_boom":     "🏛️ Citadel Economic Boom",
            "market_crash":     "💥 Market Crash",
        }
        trend_icons = {"bull": "📈", "bear": "📉", "volatile": "⚡", "stable": "➡️"}
        lines = "\n".join(f"{trend_icons[t]} **{tk}** → {t.title()}" for tk, t in trend_map.items())
        embed = discord.Embed(
            title=f"📰 Market Event: {event_names[event]}",
            description=f"Stock trends updated:\n{lines}",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="set_stock_price",
        description="[GM] Manually set a stock's price.",
    )
    @app_commands.describe(ticker="Stock ticker (e.g. ARMS)", price="New price in I.O.U.s")
    async def set_stock_price(self, interaction: discord.Interaction, ticker: str, price: int):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            cfg = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1", interaction.guild_id
            )
        gm_role_id   = cfg["gamemaster_role_id"] if cfg else None
        bot_owner_id = getattr(interaction.client, "bot_owner_id", 0)
        is_privileged = (
            (bot_owner_id and interaction.user.id == bot_owner_id)
            or interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator
            or (gm_role_id and any(r.id == gm_role_id for r in interaction.user.roles))
        )
        if not is_privileged:
            await interaction.response.send_message("❌ GMs only.", ephemeral=True)
            return
        if price < 1:
            await interaction.response.send_message("❌ Price must be at least 1.", ephemeral=True)
            return
        ticker = ticker.upper()
        async with pool.acquire() as conn:
            updated = await conn.execute(
                "UPDATE stocks SET price=$1 WHERE guild_id=$2 AND ticker=$3",
                price, interaction.guild_id, ticker,
            )
        if updated == "UPDATE 0":
            await interaction.response.send_message(f"❌ Ticker `{ticker}` not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ **{ticker}** price set to `{price}` I.O.U.s.", ephemeral=True
        )

    @app_commands.command(
        name="give_ious",
        description="[GM] Give a player I.O.U.s directly.",
    )
    @app_commands.describe(user="Target player", amount="Amount of I.O.U.s to give")
    async def give_ious(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            cfg = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1", interaction.guild_id
            )
        gm_role_id   = cfg["gamemaster_role_id"] if cfg else None
        bot_owner_id = getattr(interaction.client, "bot_owner_id", 0)
        is_privileged = (
            (bot_owner_id and interaction.user.id == bot_owner_id)
            or interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator
            or (gm_role_id and any(r.id == gm_role_id for r in interaction.user.roles))
        )
        if not is_privileged:
            await interaction.response.send_message("❌ GMs only.", ephemeral=True)
            return
        async with pool.acquire() as conn:
            await ensure_player_economy(conn, interaction.guild_id, user.id)
            await conn.execute(
                "UPDATE player_economy SET ious = ious + $1 WHERE guild_id=$2 AND owner_id=$3",
                amount, interaction.guild_id, user.id,
            )
        await interaction.response.send_message(
            f"✅ Gave `{amount}` I.O.U.s to **{user.display_name}**.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(FOBCog(bot))
