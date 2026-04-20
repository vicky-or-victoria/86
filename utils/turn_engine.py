"""
Turn engine: auto-resolves every N hours per guild.

Each turn:
  1.  Process player transit (move squadrons one step toward destination)
  2.  Apply queued GM Legion moves
  3.  AI moves unmoved Legion units (spawn + advance + attack)
  4.  Resolve combat on contested hexes
  5.  Apply supply drain to all active squadrons outside Hub A
  6.  Stamp hex controllers for uncontested occupation
  7.  Recompute hex statuses bottom-up
  8.  Clear GM move flags
  9.  Purge inactive Legion units
  10. Record turn
  11. Post summary
  12. Auto-update live map and registration embeds
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

import discord

from utils.db import get_pool
from utils.combat import resolve_combat, CombatUnit
from utils.hexmap import (
    OUTER_LABELS, SUB_POSITIONS, SAFE_HUB,
    recompute_hex_statuses, sub_addresses, outer_of, mid_of,
    is_edge_inner, adjacent_inner_clusters, adjacent_mid_clusters,
    adjacent_outer_hexes,
)

log = logging.getLogger(__name__)

# How much supply each squadron loses per turn when outside Hub A
_SUPPLY_DRAIN_PER_TURN = 1
_SUPPLY_MIN            = 0


# ── Legion unit type helper ───────────────────────────────────────────────────

def _random_unit_type() -> str:
    return random.choice(["Grauwolf", "Löwe", "Dinosauria", "Juggernaut", "Shepherd"])


def _legion_stats_for_hex(address: str) -> dict:
    """Generate Legion unit stats with slight variance based on hex depth."""
    level   = address.count("-") + 1
    base    = 8 + (level * 2)
    v       = lambda: random.randint(-2, 2)
    return dict(
        attack=base + v(), defense=base + v(), speed=base + v(),
        morale=base + v(), supply=base + v(), recon=base + v(),
    )


# ── Pushback cascade ──────────────────────────────────────────────────────────

async def _find_retreat_hex(conn, guild_id: int, lost_hex: str) -> str | None:
    """
    Walk the pushback cascade for a squadron that just lost at lost_hex.
    Returns a level-3 hex address to retreat to, or None if truly nowhere
    (caller should treat None as Final Defense trigger).

    Cascade:
      1. Another level-3 hex in the same level-2 cluster that is friendly/neutral.
      2. An adjacent level-2 cluster (ring map) in the same outer hex that is
         friendly/neutral/contested → random friendly/neutral level-3 inside it.
      3. ALL level-2 clusters in the outer hex are legion → adjacent outer hex
         (ring neighbours first, then any friendly/neutral/contested outer) →
         random friendly/neutral level-3 inside it.
      4. No valid outer hex remains → None (Final Defense).
    """
    current_mid   = mid_of(lost_hex)
    current_outer = outer_of(lost_hex)

    # ── Step 1: another level-3 in the same cluster ───────────────────────────
    l3_rows = await conn.fetch(
        "SELECT address, controller FROM hexes "
        "WHERE guild_id=$1 AND parent_address=$2 AND address != $3",
        guild_id, current_mid, lost_hex,
    )
    safe_l3 = [r["address"] for r in l3_rows if r["controller"] in ("players", "neutral")]
    if safe_l3:
        return random.choice(safe_l3)

    # ── Step 2: adjacent level-2 clusters in the same outer hex ───────────────
    adj_mids = adjacent_mid_clusters(current_mid)
    for mid_addr in adj_mids:
        mid_row = await conn.fetchrow(
            "SELECT controller FROM hexes WHERE guild_id=$1 AND address=$2",
            guild_id, mid_addr,
        )
        if mid_row and mid_row["controller"] == "legion":
            continue
        # Find friendly/neutral level-3s inside this cluster
        l3_inside = await conn.fetch(
            "SELECT address, controller FROM hexes "
            "WHERE guild_id=$1 AND parent_address=$2",
            guild_id, mid_addr,
        )
        candidates = [r["address"] for r in l3_inside if r["controller"] in ("players", "neutral")]
        if candidates:
            return random.choice(candidates)

    # ── Step 3: all level-2 clusters in the outer hex are legion-controlled ───
    # Check whether every mid in this outer is legion
    all_mids = await conn.fetch(
        "SELECT address, controller FROM hexes "
        "WHERE guild_id=$1 AND level=2 AND split_part(address,'-',1)=$2",
        guild_id, current_outer,
    )
    all_legion_outer = all(r["controller"] == "legion" for r in all_mids) if all_mids else True

    if all_legion_outer:
        # Try geometrically adjacent outer hexes first, then any remaining
        adj_outers = adjacent_outer_hexes(current_outer)
        other_outers = [o for o in OUTER_LABELS if o not in adj_outers and o != current_outer]
        ordered_outers = adj_outers + other_outers

        for outer_addr in ordered_outers:
            outer_row = await conn.fetchrow(
                "SELECT controller FROM hexes WHERE guild_id=$1 AND address=$2",
                guild_id, outer_addr,
            )
            if not outer_row or outer_row["controller"] == "legion":
                continue
            # Pick a random friendly/neutral level-3 inside this outer
            l3_rows2 = await conn.fetch(
                "SELECT address, controller FROM hexes "
                "WHERE guild_id=$1 AND level=3 AND split_part(address,'-',1)=$2",
                guild_id, outer_addr,
            )
            candidates = [r["address"] for r in l3_rows2 if r["controller"] in ("players", "neutral")]
            if candidates:
                return random.choice(candidates)

        # Nothing found → Final Defense
        return None

    # The outer hex still has non-legion clusters but none adjacent were usable —
    # fall back to any non-legion level-3 in the same outer hex
    all_l3 = await conn.fetch(
        "SELECT address, controller FROM hexes "
        "WHERE guild_id=$1 AND level=3 AND split_part(address,'-',1)=$2",
        guild_id, current_outer,
    )
    candidates = [r["address"] for r in all_l3 if r["controller"] in ("players", "neutral")]
    if candidates:
        return random.choice(candidates)

    return None  # truly nowhere → Final Defense


async def _trigger_final_defense(conn, guild_id: int, summaries: list):
    """
    All outer hexes B–G are legion-controlled.
    - Allow Legion to enter Hex A (gradually, via normal AI movement next turns).
    - Scatter every surviving player squadron to a random level-3 hex inside Hex A.
    """
    log.warning(f"FINAL DEFENSE triggered for guild {guild_id}")

    # Fetch all level-3 hexes inside Hex A
    hub_l3 = await conn.fetch(
        "SELECT address FROM hexes WHERE guild_id=$1 AND level=3 AND split_part(address,'-',1)=$2",
        guild_id, SAFE_HUB,
    )
    hub_addresses = [r["address"] for r in hub_l3]
    if not hub_addresses:
        # Fallback: generate them manually
        hub_addresses = [f"{SAFE_HUB}-{m}-{i}" for m in SUB_POSITIONS for i in SUB_POSITIONS]

    # Scatter each active squadron individually to a random Hex A level-3
    active_squadrons = await conn.fetch(
        "SELECT id, name, owner_name FROM squadrons "
        "WHERE guild_id=$1 AND is_active=TRUE",
        guild_id,
    )
    for sq in active_squadrons:
        dest = random.choice(hub_addresses)
        await conn.execute(
            "UPDATE squadrons SET hex_address=$1, home_outer=$2, "
            "in_transit=FALSE, transit_destination=NULL, transit_step=0 WHERE id=$3",
            dest, SAFE_HUB, sq["id"],
        )
        summaries.append(f"🏰 **{sq['owner_name']}'s {sq['name']}** pulled back to `{dest}`.")

    # Ensure the column exists (safe for live DBs that predate this migration)
    await conn.execute(
        "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS citadel_besieged BOOLEAN NOT NULL DEFAULT FALSE"
    )
    await conn.execute(
        "UPDATE guild_config SET citadel_besieged=TRUE WHERE guild_id=$1", guild_id
    )

    summaries.insert(
        0,
        "☠️ **THE FINAL DEFENSE IN THE CITADEL** — The Legion has breached every outer hex. "
        "All squadrons have fallen back to Hub A. The Citadel must hold.",
    )


# ── Turn engine ───────────────────────────────────────────────────────────────

class TurnEngine:
    def __init__(self, bot):
        self.bot  = bot
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
                "SELECT guild_id, turn_interval_hours, last_turn_at, game_started "
                "FROM guild_config"
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
        summaries   = []

        async with conn.transaction():
            # 1. Process player transit
            await self._process_transit(conn, guild_id, summaries)

            # 2. Apply queued GM Legion moves
            await self._apply_gm_moves(conn, guild_id, summaries)

            # 3. AI moves unmoved Legion units
            await self._legion_ai(conn, guild_id, summaries)

            # 4. Resolve combat
            await self._resolve_combat(conn, guild_id, turn_number, summaries)

            # 5. Supply drain
            await self._apply_supply_drain(conn, guild_id, summaries)

            # 6. Stamp hex controllers for uncontested occupation
            await self._apply_occupation(conn, guild_id)

            # 7. Recompute hex statuses
            await recompute_hex_statuses(conn, guild_id)

            # 7. Clear GM move flags
            await conn.execute(
                "UPDATE legion_units SET manually_moved=FALSE WHERE guild_id=$1", guild_id
            )
            await conn.execute(
                "DELETE FROM legion_gm_moves WHERE guild_id=$1", guild_id
            )

            # 8. Purge inactive Legion units
            deleted = await conn.execute(
                "DELETE FROM legion_units WHERE guild_id=$1 AND is_active=FALSE", guild_id
            )
            log.debug(f"Purged inactive Legion units for guild {guild_id}: {deleted}")

            # 9. Record turn
            await conn.execute(
                "INSERT INTO turn_history (guild_id, turn_number) VALUES ($1,$2)",
                guild_id, turn_number,
            )
            await conn.execute(
                "UPDATE guild_config SET last_turn_at=NOW() WHERE guild_id=$1", guild_id
            )

        await self._post_summary(guild_id, turn_number, summaries)

        # Economy: fluctuate stocks and apply Command Bunker passive I.O.U. income
        try:
            from cogs.fob_cog import fluctuate_stocks, apply_bunker_income
            pool2 = await get_pool()
            async with pool2.acquire() as econ_conn:
                await fluctuate_stocks(econ_conn, guild_id)
                await apply_bunker_income(econ_conn, guild_id)
        except Exception as _e:
            log.warning(f"FOB economy tick failed: {_e}")

        # Auto-update the live map embed
        try:
            from cogs.map_cog import auto_update_map
            await auto_update_map(self.bot, guild_id)
        except Exception as e:
            log.warning(f"auto_update_map failed for guild {guild_id}: {e}")

        # Auto-update the registration embed
        try:
            from cogs.squadron_cog import update_registration_embed
            await update_registration_embed(self.bot, guild_id)
        except Exception as e:
            log.warning(f"update_registration_embed failed for guild {guild_id}: {e}")

    # ── Player transit ────────────────────────────────────────────────────────
    async def _process_transit(self, conn, guild_id: int, summaries: list):
        from utils.hexmap import entry_hex_for_outer, SAFE_HUB, outer_of
        in_transit = await conn.fetch(
            "SELECT id, name, owner_name, hex_address, transit_destination, transit_step "
            "FROM squadrons WHERE guild_id=$1 AND in_transit=TRUE AND is_active=TRUE",
            guild_id,
        )
        for sq in in_transit:
            step = sq["transit_step"]
            dest = sq["transit_destination"]
            if step == 1:
                current_outer = outer_of(sq["hex_address"])
                hub_entry     = entry_hex_for_outer(SAFE_HUB, current_outer)
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1, transit_step=2, home_outer=$2 WHERE id=$3",
                    hub_entry, SAFE_HUB, sq["id"],
                )
                summaries.append(
                    f"🚶 **{sq['owner_name']}'s {sq['name']}** arrived at Hub A "
                    f"(`{hub_entry}`, en route to Hex {dest})."
                )
            elif step == 2:
                dest_outer = outer_of(dest)
                await conn.execute(
                    "UPDATE squadrons SET hex_address=$1, home_outer=$2, "
                    "in_transit=FALSE, transit_destination=NULL, transit_step=0 WHERE id=$3",
                    dest, dest_outer, sq["id"],
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
            guild_id,
        )
        for move in moves:
            await conn.execute(
                "UPDATE legion_units SET hex_address=$1, manually_moved=TRUE WHERE id=$2",
                move["target_address"], move["legion_unit_id"],
            )
            summaries.append(
                f"🎮 **Legion {move['unit_type']}** moved to **{move['target_address']}** (GM order)."
            )

    # ── Legion AI ─────────────────────────────────────────────────────────────
    async def _legion_ai(self, conn, guild_id: int, summaries: list):
        """
        Legion units live at level 3.
        - Spawn: random level-3 hex inside a neutral outer hex (not A, unless besieged).
        - Advance: prefer neutral > player > legion hexes.
        """
        # Check if citadel is besieged (Final Defense active)
        try:
            cfg = await conn.fetchrow(
                "SELECT citadel_besieged FROM guild_config WHERE guild_id=$1", guild_id
            )
            citadel_besieged = bool(cfg["citadel_besieged"]) if cfg and cfg["citadel_besieged"] is not None else False
        except Exception:
            citadel_besieged = False

        # ── Spawn ─────────────────────────────────────────────────────────────
        neutral_outer = await conn.fetch(
            "SELECT address FROM hexes "
            "WHERE guild_id=$1 AND level=1 AND controller='neutral' AND address != $2",
            guild_id, SAFE_HUB,
        )
        spawn_candidates = [r["address"] for r in neutral_outer]
        random.shuffle(spawn_candidates)
        for outer_addr in spawn_candidates[:2]:
            mid_choice   = random.choice(SUB_POSITIONS)
            inner_choice = random.choice(SUB_POSITIONS)
            spawn_addr   = f"{outer_addr}-{mid_choice}-{inner_choice}"
            stats        = _legion_stats_for_hex(spawn_addr)
            unit_type    = _random_unit_type()
            await conn.execute(
                """INSERT INTO legion_units
                   (guild_id, unit_type, hex_address,
                    attack, defense, speed, morale, supply, recon, manually_moved)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)""",
                guild_id, unit_type, spawn_addr,
                stats["attack"], stats["defense"], stats["speed"],
                stats["morale"], stats["supply"], stats["recon"],
            )
            summaries.append(f"🔴 **Legion {unit_type}** spawned at `{spawn_addr}`.")

        # ── Move existing unmoved units ───────────────────────────────────────
        units = await conn.fetch(
            "SELECT id, hex_address, unit_type FROM legion_units "
            "WHERE guild_id=$1 AND is_active=TRUE AND manually_moved=FALSE",
            guild_id,
        )
        for unit in units:
            addr       = unit["hex_address"]
            unit_outer = outer_of(addr)

            # Skip Hub A unless citadel is besieged
            if unit_outer == SAFE_HUB and not citadel_besieged:
                continue

            current_mid = mid_of(addr)

            # Build candidates: same cluster + adjacent clusters
            candidates = []
            for pos in SUB_POSITIONS:
                candidate = f"{current_mid}-{pos}"
                if candidate != addr:
                    candidates.append(candidate)

            if is_edge_inner(addr):
                for adj_mid in adjacent_inner_clusters(addr):
                    if outer_of(adj_mid) == SAFE_HUB and not citadel_besieged:
                        continue
                    candidates.append(f"{adj_mid}-C")

            if not candidates:
                continue

            ctrl_rows = await conn.fetch(
                "SELECT address, controller FROM hexes "
                "WHERE guild_id=$1 AND address = ANY($2::text[])",
                guild_id, candidates,
            )
            ctrl_map = {r["address"]: r["controller"] for r in ctrl_rows}

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
                target, unit["id"],
            )

    # ── Combat resolution ─────────────────────────────────────────────────────
    async def _resolve_combat(self, conn, guild_id: int, turn_number: int, summaries: list):
        player_rows = await conn.fetch(
            "SELECT hex_address, owner_name, name, attack, defense, speed, morale, supply, recon "
            "FROM squadrons WHERE guild_id=$1 AND is_active=TRUE AND in_transit=FALSE",
            guild_id,
        )
        legion_rows = await conn.fetch(
            "SELECT id, hex_address, unit_type, attack, defense, speed, morale, supply, recon "
            "FROM legion_units WHERE guild_id=$1 AND is_active=TRUE",
            guild_id,
        )

        player_by_hex: dict[str, list] = {}
        for p in player_rows:
            player_by_hex.setdefault(p["hex_address"], []).append(p)

        legion_by_hex: dict[str, list] = {}
        for l in legion_rows:
            legion_by_hex.setdefault(l["hex_address"], []).append(l)

        contested = set(player_by_hex.keys()) & set(legion_by_hex.keys())

        # Track whether Final Defense needs to fire after all combats resolve
        final_defense_needed = False

        for hex_addr in contested:
            p_units = player_by_hex[hex_addr]
            l_units = legion_by_hex[hex_addr]

            def avg(stat):
                return sum(u[stat] for u in p_units) // len(p_units)

            player_unit = CombatUnit(
                name=f"Allied Squadrons ({', '.join(u['name'] for u in p_units)})",
                side="players",
                attack=avg("attack"), defense=avg("defense"), speed=avg("speed"),
                morale=avg("morale"), supply=avg("supply"), recon=avg("recon"),
            )

            fatigue_stacks = 0
            final_ctrl     = "neutral"
            player_routed  = False

            for l in l_units:
                legion_unit = CombatUnit(
                    name=f"Legion {l['unit_type']}",
                    side="legion",
                    attack=l["attack"], defense=l["defense"], speed=l["speed"],
                    morale=l["morale"], supply=l["supply"], recon=l["recon"],
                    unit_type=l["unit_type"],
                )

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

                await conn.execute(
                    """INSERT INTO combat_log
                       (guild_id, turn_number, hex_address, attacker, defender,
                        attacker_roll, defender_roll, outcome)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    guild_id, turn_number, hex_addr,
                    result.attacker, result.defender,
                    result.attacker_roll, result.defender_roll, result.outcome,
                )
                summaries.append(f"⚔️ **Hex {hex_addr}**: {result.narrative}")

                if result.outcome == "attacker_wins":
                    final_ctrl = "players"
                    await conn.execute(
                        "UPDATE legion_units SET is_active=FALSE WHERE id=$1", l["id"]
                    )

                elif result.outcome == "defender_wins":
                    final_ctrl     = "legion"
                    player_routed  = True
                    fatigue_stacks += 1
                    break  # routed — stop fighting remaining Legion units on this hex

                else:  # draw
                    final_ctrl     = "neutral"
                    fatigue_stacks += 1

            # Write hex controller ONCE after all fights on this hex are done
            await conn.execute(
                "UPDATE hexes SET controller=$1 WHERE guild_id=$2 AND address=$3",
                final_ctrl, guild_id, hex_addr,
            )

            # ── Award raw materials to players who survived/won combat ────────
            try:
                from cogs.fob_cog import award_combat_raw_materials
                for p_unit in p_units:
                    # Look up owner_id for this squadron
                    sq_row = await conn.fetchrow(
                        "SELECT owner_id FROM squadrons WHERE guild_id=$1 AND name=$2 AND is_active=TRUE LIMIT 1",
                        guild_id, p_unit["name"],
                    )
                    if sq_row:
                        won = (final_ctrl == "players")
                        await award_combat_raw_materials(conn, guild_id, sq_row["owner_id"], won)
            except Exception as _e:
                log.warning(f"FOB raw material award failed: {_e}")

            # ── Pushback on player rout ───────────────────────────────────────
            if player_routed:
                retreat_hex = await _find_retreat_hex(conn, guild_id, hex_addr)

                if retreat_hex is None:
                    # No valid retreat exists — Final Defense
                    final_defense_needed = True
                else:
                    # Move the whole group to the same retreat hex
                    sq_ids = await conn.fetch(
                        "SELECT id, name, owner_name FROM squadrons "
                        "WHERE guild_id=$1 AND is_active=TRUE AND hex_address=$2",
                        guild_id, hex_addr,
                    )
                    for sq in sq_ids:
                        await conn.execute(
                            "UPDATE squadrons SET hex_address=$1, home_outer=$2 WHERE id=$3",
                            retreat_hex, outer_of(retreat_hex), sq["id"],
                        )
                    summaries.append(
                        f"🔙 **Allied Squadrons** routed from `{hex_addr}` → fell back to `{retreat_hex}`."
                    )

        # ── Final Defense check ───────────────────────────────────────────────
        if not final_defense_needed:
            # Also check the persistent endgame condition (all B–G legion-controlled)
            outer_rows = await conn.fetch(
                "SELECT status FROM hexes WHERE guild_id=$1 AND level=1 AND address != $2",
                guild_id, SAFE_HUB,
            )
            from utils.hexmap import STATUS_LEGION, STATUS_MAJ_LEGION
            _legion_statuses = {STATUS_LEGION, STATUS_MAJ_LEGION}
            if outer_rows and all(r["status"] in _legion_statuses for r in outer_rows):
                final_defense_needed = True

        if final_defense_needed:
            # Only trigger if not already besieged
            try:
                cfg = await conn.fetchrow(
                    "SELECT citadel_besieged FROM guild_config WHERE guild_id=$1", guild_id
                )
                already = bool(cfg["citadel_besieged"]) if cfg and cfg["citadel_besieged"] is not None else False
            except Exception:
                already = False
            if not already:
                await _trigger_final_defense(conn, guild_id, summaries)

    # ── Hex occupation ───────────────────────────────────────────────────────
    async def _apply_occupation(self, conn, guild_id: int):
        """
        For every level-3 hex that has at least one occupying unit and no
        opposing unit, set the controller to match the occupier.

        This is the mechanism by which squadrons and Legion units actually
        capture hexes — combat only fires on contested hexes, so uncontested
        presence must be handled separately.

        Priority:
          - A hex occupied ONLY by players  → controller = 'players'
          - A hex occupied ONLY by legion   → controller = 'legion'
          - A hex occupied by BOTH          → leave as-is (combat handles it)
          - A hex with NO occupiers         → leave as-is (controller persists)
        """
        player_rows = await conn.fetch(
            "SELECT hex_address FROM squadrons "
            "WHERE guild_id=$1 AND is_active=TRUE AND in_transit=FALSE",
            guild_id,
        )
        legion_rows = await conn.fetch(
            "SELECT hex_address FROM legion_units WHERE guild_id=$1 AND is_active=TRUE",
            guild_id,
        )

        player_hexes = {r["hex_address"] for r in player_rows}
        legion_hexes = {r["hex_address"] for r in legion_rows}

        # Uncontested player hexes
        for addr in player_hexes - legion_hexes:
            await conn.execute(
                "UPDATE hexes SET controller='players' WHERE guild_id=$1 AND address=$2",
                guild_id, addr,
            )

        # Uncontested legion hexes
        for addr in legion_hexes - player_hexes:
            await conn.execute(
                "UPDATE hexes SET controller='legion' WHERE guild_id=$1 AND address=$2",
                guild_id, addr,
            )

    # ── Supply drain ──────────────────────────────────────────────────────────
    async def _apply_supply_drain(self, conn, guild_id: int, summaries: list):
        """
        Drain 1 supply per turn from every active squadron that is outside Hub A.
        Supply floors at 0. Squadrons at 0 supply are penalised in combat (-2 rolls).
        Players must scavenge in the field to recover supply.
        """
        drained = await conn.fetch(
            "SELECT id, name, owner_name, supply FROM squadrons "
            "WHERE guild_id=$1 AND is_active=TRUE AND in_transit=FALSE "
            "AND split_part(hex_address,'-',1) != $2",
            guild_id, SAFE_HUB,
        )
        for sq in drained:
            new_supply = max(_SUPPLY_MIN, sq["supply"] - _SUPPLY_DRAIN_PER_TURN)
            await conn.execute(
                "UPDATE squadrons SET supply=$1 WHERE id=$2",
                new_supply, sq["id"],
            )
            if new_supply <= 4:
                summaries.append(
                    f"⚠️ **{sq['owner_name']}'s {sq['name']}** is running low on supply "
                    f"(`{new_supply}` remaining) — combat penalty active. Scavenge to resupply."
                )

    # ── Post summary ──────────────────────────────────────────────────────────
    async def _post_summary(self, guild_id: int, turn_number: int, summaries: list):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

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
