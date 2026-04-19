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
# Imported lazily inside _resolve_turn to avoid circular import at module load
# (map_cog imports turn_engine indirectly via the bot; we only call it at runtime)

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

            # 7. Purge inactive Legion units to prevent table bloat
            deleted = await conn.execute(
                "DELETE FROM legion_units WHERE guild_id=$1 AND is_active=FALSE", guild_id
            )
            log.debug(f"Purged inactive Legion units for guild {guild_id}: {deleted}")

            # 9. Record turn
            await conn.execute(
                "INSERT INTO turn_history (guild_id, turn_number) VALUES ($1,$2)",
                guild_id, turn_number
            )
            await conn.execute(
                "UPDATE guild_config SET last_turn_at=NOW() WHERE guild_id=$1", guild_id
            )

        await self._post_summary(guild_id, turn_number, summaries)

        # Auto-update the live map embed (if one has been posted this session)
        try:
            from cogs.map_cog import auto_update_map
            await auto_update_map(self.bot, guild_id)
        except Exception as e:
            log.warning(f"auto_update_map failed for guild {guild_id}: {e}")

    # ── Player transit ────────────────────────────────────────────────────────
    async def _process_transit(self, conn, guild_id: int, summaries: list):
        from utils.hexmap import entry_hex_for_outer, SAFE_HUB, outer_of
        in_transit = await conn.fetch(
            "SELECT id, name, owner_name, hex_address, transit_destination, transit_step "
            "FROM squadrons WHERE guild_id=$1 AND in_transit=TRUE AND is_active=TRUE",
            guild_id
        )
        for sq in in_transit:
            step = sq["transit_step"]
            dest = sq["transit_destination"]
            if step == 1:
                # Step 1: move to Hub A — land at the facing corner of A
                current_outer = outer_of(sq["hex_address"])
                hub_entry = entry_hex_for_outer(SAFE_HUB, current_outer)
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1, transit_step=2, home_outer=$2 "
                    "WHERE id=$3",
                    hub_entry, SAFE_HUB, sq["id"]
                )
                summaries.append(
                    f"🚶 **{sq['owner_name']}'s {sq['name']}** arrived at Hub A "
                    f"(`{hub_entry}`, en route to Hex {dest})."
                )
            elif step == 2:
                # Step 2: move to the final destination (already a level-3 address)
                dest_outer = outer_of(dest)
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1, home_outer=$2, "
                    "in_transit=FALSE, transit_destination=NULL, transit_step=0 "
                    "WHERE id=$3",
                    dest, dest_outer, sq["id"]
                )
                summaries.append(
                    f"✅ **{sq['owner_name']}'s {sq['name']}** deployed to `{dest}`."
                )

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
        Legion units live exclusively at level 3.
        - Spawn: pick a random level-3 hex inside a neutral outer hex (not A).
        - Advance: move to an adjacent level-3 hex within the same outer hex,
          preferring neutral > player-controlled. Can cross cluster boundaries
          if at an edge inner position.
        Legion never enters any hex inside Hex A.
        """
        from utils.hexmap import (
            SAFE_HUB, outer_of, mid_of, inner_pos,
            is_edge_inner, adjacent_inner_clusters, sub_addresses, SUB_POSITIONS
        )

        # ── Spawn on neutral outer hexes (B–G only) ──────────────────────────
        neutral_outer = await conn.fetch(
            "SELECT address FROM hexes "
            "WHERE guild_id=$1 AND level=1 AND controller='neutral' AND address != $2",
            guild_id, SAFE_HUB
        )
        spawn_candidates = [r["address"] for r in neutral_outer]
        random.shuffle(spawn_candidates)
        for outer_addr in spawn_candidates[:2]:
            # Pick a random level-3 hex inside this outer hex
            mid_pos_choice = random.choice(SUB_POSITIONS)
            inner_pos_choice = random.choice(SUB_POSITIONS)
            spawn_addr = f"{outer_addr}-{mid_pos_choice}-{inner_pos_choice}"
            unit = legion_unit_for_hex(spawn_addr)
            await conn.execute(
                """INSERT INTO legion_units
                   (guild_id, unit_type, hex_address, attack, defense, speed, morale, supply, recon, manually_moved)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)""",
                guild_id, _random_unit_type(), spawn_addr,
                unit.attack, unit.defense, unit.speed,
                unit.morale, unit.supply, unit.recon
            )
            summaries.append(f"🔴 **Legion** spawned at `{spawn_addr}`.")

        # ── Move existing unmoved units ───────────────────────────────────────
        units = await conn.fetch(
            "SELECT id, hex_address, unit_type FROM legion_units "
            "WHERE guild_id=$1 AND is_active=TRUE AND manually_moved=FALSE",
            guild_id
        )
        for unit in units:
            addr = unit["hex_address"]
            if outer_of(addr) == SAFE_HUB:
                continue  # never enter Hex A

            current_mid = mid_of(addr)

            # Build candidate level-3 hexes to move into:
            # 1. Other positions within same cluster
            candidates = []
            for pos in SUB_POSITIONS:
                candidate = f"{current_mid}-{pos}"
                if candidate != addr:
                    candidates.append(candidate)

            # 2. If at an edge inner hex, also consider adjacent clusters
            if is_edge_inner(addr):
                adj_mids = adjacent_inner_clusters(addr)
                for adj_mid in adj_mids:
                    if outer_of(adj_mid) == SAFE_HUB:
                        continue
                    # Enter at the center of the adjacent cluster
                    candidates.append(f"{adj_mid}-C")

            if not candidates:
                continue

            # Fetch controllers for all candidates
            ctrl_rows = await conn.fetch(
                "SELECT address, controller FROM hexes "
                "WHERE guild_id=$1 AND address = ANY($2::text[])",
                guild_id, candidates
            )
            ctrl_map = {r["address"]: r["controller"] for r in ctrl_rows}

            # Prefer neutral, then player (attack), skip legion-only hexes unless no choice
            neutrals = [c for c in candidates if ctrl_map.get(c) == "neutral"]
            players  = [c for c in candidates if ctrl_map.get(c) == "players"]
            others   = [c for c in candidates if ctrl_map.get(c) not in ("neutral", "players")]

            if neutrals:
                target = random.choice(neutrals)
            elif players:
                target = random.choice(players)
            else:
                target = random.choice(others) if others else random.choice(candidates)

            await conn.execute(
                "UPDATE legion_units SET hex_address=$1 WHERE id=$2",
                target, unit["id"]
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

            # Fight each Legion unit in the hex sequentially.
            # Each fight that isn't a clean win drains the player unit's
            # effective attack and morale (battle fatigue), making
            # multi-Legion hexes progressively harder to clear.
            fatigue_stacks = 0
            for l in l_units:
                legion_unit = CombatUnit(
                    name=f"Legion {l['unit_type']}",
                    side="legion",
                    attack=l["attack"], defense=l["defense"], speed=l["speed"],
                    morale=l["morale"], supply=l["supply"], recon=l["recon"],
                )

                # Apply accumulated fatigue to a copy of the player unit
                fatigued_player = CombatUnit(
                    name=player_unit.name,
                    side=player_unit.side,
                    attack=max(1, player_unit.attack - fatigue_stacks * 2),
                    defense=player_unit.defense,
                    speed=player_unit.speed,
                    morale=max(1, player_unit.morale - fatigue_stacks),
                    supply=player_unit.supply,
                    recon=player_unit.recon,
                )

                result = resolve_combat(fatigued_player, legion_unit)

                if result.outcome == "attacker_wins":
                    new_ctrl = "players"
                    await conn.execute(
                        "UPDATE legion_units SET is_active=FALSE WHERE id=$1", l["id"]
                    )
                elif result.outcome == "defender_wins":
                    new_ctrl = "legion"
                    fatigue_stacks += 1  # loss accumulates fatigue for remaining fights
                else:
                    new_ctrl = "neutral"  # draw — stays contested
                    fatigue_stacks += 1  # draw also wears the unit down

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
