"""
Turn engine: auto-resolves every N hours per guild.

Each turn:
  1. Process player transit (move squadrons one step toward destination)
  2. Apply queued GM Legion moves
  3. AI moves unmoved Legion units (spawn + advance + attack)
  4. Resolve combat on contested hexes
  5. Recompute hex statuses bottom-up
  6. Clear GM move flags
  7. Post summary
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

import discord

from utils.db import get_pool
from utils.combat import resolve_combat, legion_unit_for_hex, CombatUnit
from utils.hexmap import (
    OUTER_LABELS, SUB_POSITIONS, SAFE_HUB,
    recompute_hex_statuses, sub_addresses, outer_of
)

log = logging.getLogger(__name__)


class TurnEngine:
    def __init__(self, bot):
        self.bot = bot
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await self._tick_all_guilds()
            except Exception as e:
                log.error(f"Turn engine error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _tick_all_guilds(self):
        pool = await get_pool()
        async with pool.acquire() as conn:
            guilds = await conn.fetch(
                "SELECT guild_id, turn_interval_hours, last_turn_at, game_started FROM guild_config"
            )
            now = datetime.now(timezone.utc)
            for g in guilds:
                if not g["game_started"]:
                    continue
                delta = now - g["last_turn_at"].replace(tzinfo=timezone.utc)
                if delta.total_seconds() / 3600 >= g["turn_interval_hours"]:
                    await self._resolve_turn(conn, g["guild_id"])

    async def _resolve_turn(self, conn, guild_id: int):
        log.info(f"Resolving turn for guild {guild_id}")
        turn_row = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM turn_history WHERE guild_id=$1", guild_id
        )
        turn_number = (turn_row["cnt"] or 0) + 1
        summaries = []

        async with conn.transaction():
            # 1. Process player transit
            await self._process_transit(conn, guild_id, summaries)

            # 2. Apply queued GM legion moves
            await self._apply_gm_moves(conn, guild_id, summaries)

            # 3. AI moves unmoved Legion units
            await self._legion_ai(conn, guild_id, summaries)

            # 4. Resolve combat
            await self._resolve_combat(conn, guild_id, turn_number, summaries)

            # 5. Recompute hex statuses
            await recompute_hex_statuses(conn, guild_id)

            # 6. Clear GM move flags
            await conn.execute(
                "UPDATE legion_units SET manually_moved=FALSE WHERE guild_id=$1", guild_id
            )
            await conn.execute(
                "DELETE FROM legion_gm_moves WHERE guild_id=$1", guild_id
            )

            # 7. Record turn
            await conn.execute(
                "INSERT INTO turn_history (guild_id, turn_number) VALUES ($1,$2)",
                guild_id, turn_number
            )
            await conn.execute(
                "UPDATE guild_config SET last_turn_at=NOW() WHERE guild_id=$1", guild_id
            )

        await self._post_summary(guild_id, turn_number, summaries)

    # ── Player transit ────────────────────────────────────────────────────────
    async def _process_transit(self, conn, guild_id: int, summaries: list):
        in_transit = await conn.fetch(
            "SELECT id, name, owner_name, hex_address, transit_destination, transit_step "
            "FROM squadrons WHERE guild_id=$1 AND in_transit=TRUE AND is_active=TRUE",
            guild_id
        )
        for sq in in_transit:
            step = sq["transit_step"]
            dest = sq["transit_destination"]
            if step == 1:
                # Step 1: move to Hex A
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1, transit_step=2, home_outer=$2 "
                    "WHERE id=$3",
                    SAFE_HUB, SAFE_HUB, sq["id"]
                )
                summaries.append(f"🚶 **{sq['owner_name']}'s {sq['name']}** arrived at Hex A (en route to {dest}).")
            elif step == 2:
                # Step 2: move from A to destination
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1, home_outer=$1, "
                    "in_transit=FALSE, transit_destination=NULL, transit_step=0 "
                    "WHERE id=$2",
                    dest, sq["id"]
                )
                summaries.append(f"✅ **{sq['owner_name']}'s {sq['name']}** deployed to Hex {dest}.")

    # ── GM Legion moves ───────────────────────────────────────────────────────
    async def _apply_gm_moves(self, conn, guild_id: int, summaries: list):
        moves = await conn.fetch(
            "SELECT gm.legion_unit_id, gm.target_address, lu.unit_type "
            "FROM legion_gm_moves gm "
            "JOIN legion_units lu ON lu.id = gm.legion_unit_id "
            "WHERE gm.guild_id=$1",
            guild_id
        )
        for move in moves:
            await conn.execute(
                "UPDATE legion_units SET hex_address=$1, manually_moved=TRUE WHERE id=$2",
                move["target_address"], move["legion_unit_id"]
            )
            summaries.append(f"🎮 **Legion {move['unit_type']}** moved to **{move['target_address']}** (GM order).")

    # ── Legion AI ─────────────────────────────────────────────────────────────
    async def _legion_ai(self, conn, guild_id: int, summaries: list):
        """
        For each Legion unit not manually moved this turn:
          - If on outer hex: try to advance into a mid hex inside it
          - If on mid hex: try to advance into an inner hex
          - If on inner hex: stay (already at deepest)
        Also spawn new units on neutral outer hexes (B-G only).
        Legion never enters Hex A.
        """
        # Spawn on neutral outer hexes (B-G)
        neutral_outer = await conn.fetch(
            "SELECT address FROM hexes "
            "WHERE guild_id=$1 AND level=1 AND controller='neutral' AND address != $2",
            guild_id, SAFE_HUB
        )
        spawn_candidates = [r["address"] for r in neutral_outer]
        random.shuffle(spawn_candidates)
        for addr in spawn_candidates[:2]:
            unit = legion_unit_for_hex(addr)
            await conn.execute(
                """INSERT INTO legion_units
                   (guild_id, unit_type, hex_address, attack, defense, speed, morale, supply, recon, manually_moved)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)""",
                guild_id, _random_unit_type(), addr,
                unit.attack, unit.defense, unit.speed,
                unit.morale, unit.supply, unit.recon
            )
            summaries.append(f"🔴 **Legion** spawned at outer Hex **{addr}**.")

        # Move existing unmoved units inward
        units = await conn.fetch(
            "SELECT id, hex_address, unit_type FROM legion_units "
            "WHERE guild_id=$1 AND is_active=TRUE AND manually_moved=FALSE",
            guild_id
        )
        for unit in units:
            addr = unit["hex_address"]
            outer = outer_of(addr)
            if outer == SAFE_HUB:
                continue  # never enter A

            level = addr.count("-") + 1
            if level == 3:
                continue  # already at deepest

            # Find child hexes that aren't player-controlled
            children = await conn.fetch(
                "SELECT address, controller FROM hexes "
                "WHERE guild_id=$1 AND parent_address=$2 AND controller != 'players'",
                guild_id, addr
            )
            if not children:
                # All children are player-controlled — attack anyway
                children = await conn.fetch(
                    "SELECT address, controller FROM hexes "
                    "WHERE guild_id=$1 AND parent_address=$2",
                    guild_id, addr
                )
            if not children:
                continue

            # Prefer neutral, then player
            neutrals = [c for c in children if c["controller"] == "neutral"]
            target = random.choice(neutrals) if neutrals else random.choice(list(children))
            await conn.execute(
                "UPDATE legion_units SET hex_address=$1 WHERE id=$2",
                target["address"], unit["id"]
            )

    # ── Combat resolution ─────────────────────────────────────────────────────
    async def _resolve_combat(self, conn, guild_id: int, turn_number: int, summaries: list):
        player_rows = await conn.fetch(
            "SELECT hex_address, owner_name, name, attack, defense, speed, morale, supply, recon "
            "FROM squadrons WHERE guild_id=$1 AND is_active=TRUE AND in_transit=FALSE",
            guild_id
        )
        legion_rows = await conn.fetch(
            "SELECT id, hex_address, unit_type, attack, defense, speed, morale, supply, recon "
            "FROM legion_units WHERE guild_id=$1 AND is_active=TRUE",
            guild_id
        )

        player_by_hex: dict[str, list] = {}
        for p in player_rows:
            # Skip if in safe hub (Hex A) — no combat there
            if p["hex_address"] == SAFE_HUB:
                continue
            player_by_hex.setdefault(p["hex_address"], []).append(p)

        legion_by_hex: dict[str, list] = {}
        for l in legion_rows:
            if l["hex_address"] == SAFE_HUB:
                continue
            legion_by_hex.setdefault(l["hex_address"], []).append(l)

        contested = set(player_by_hex.keys()) & set(legion_by_hex.keys())

        for hex_addr in contested:
            p_units = player_by_hex[hex_addr]
            l_units = legion_by_hex[hex_addr]

            avg = lambda stat: sum(u[stat] for u in p_units) // len(p_units)
            player_unit = CombatUnit(
                name=f"Allied Squadrons ({', '.join(u['name'] for u in p_units)})",
                side="players",
                attack=avg("attack"), defense=avg("defense"), speed=avg("speed"),
                morale=avg("morale"), supply=avg("supply"), recon=avg("recon"),
            )

            # Fight each Legion unit in the hex sequentially
            for l in l_units:
                legion_unit = CombatUnit(
                    name=f"Legion {l['unit_type']}",
                    side="legion",
                    attack=l["attack"], defense=l["defense"], speed=l["speed"],
                    morale=l["morale"], supply=l["supply"], recon=l["recon"],
                )

                result = resolve_combat(player_unit, legion_unit)

                if result.outcome == "attacker_wins":
                    new_ctrl = "players"
                    await conn.execute(
                        "UPDATE legion_units SET is_active=FALSE WHERE id=$1", l["id"]
                    )
                elif result.outcome == "defender_wins":
                    new_ctrl = "legion"
                else:
                    new_ctrl = "neutral"  # draw — stays contested

                await conn.execute(
                    "UPDATE hexes SET controller=$1 WHERE guild_id=$2 AND address=$3",
                    new_ctrl, guild_id, hex_addr
                )
                await conn.execute(
                    """INSERT INTO combat_log
                       (guild_id, turn_number, hex_address, attacker, defender,
                        attacker_roll, defender_roll, outcome)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    guild_id, turn_number, hex_addr,
                    result.attacker, result.defender,
                    result.attacker_roll, result.defender_roll, result.outcome
                )
                summaries.append(f"⚔️ **Hex {hex_addr}**: {result.narrative}")

    # ── Post summary ──────────────────────────────────────────────────────────
    async def _post_summary(self, guild_id: int, turn_number: int, summaries: list):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        # Use the configured report channel if set, otherwise fall back to first writable channel
        pool = await get_pool()
        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT report_channel_id FROM guild_config WHERE guild_id=$1", guild_id
            )

        channel = None
        if config and config["report_channel_id"]:
            channel = guild.get_channel(config["report_channel_id"])

        if channel is None:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break

        if channel is None:
            log.warning(f"No writable channel found for guild {guild_id}")
            return

        embed = discord.Embed(
            title=f"⚔️ Turn {turn_number} — After Action Report",
            color=discord.Color.red(),
            description="\n".join(summaries) if summaries else "No activity this turn.",
        )
        embed.set_footer(text="86 — Eighty Six | The Legion never stops.")
        await channel.send(embed=embed)


def _random_unit_type() -> str:
    return random.choice(["Grauwolf", "Löwe", "Dinosauria", "Juggernaut", "Shepherd"])
