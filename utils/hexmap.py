"""
Hex addressing scheme:
  Level 1 (outer):  A–G  (center=A, ring positions 1–6 = B–G)
  Level 2 (mid):    A-C (center), A-1 through A-6
  Level 3 (inner):  A-1-C, A-1-1 through A-1-6
"""

from typing import Optional

OUTER_LABELS = ["A", "B", "C", "D", "E", "F", "G"]
SUB_POSITIONS = ["C", "1", "2", "3", "4", "5", "6"]

# Hex A is always player-controlled at game start
SAFE_HUB = "A"

# Status labels derived from child hex counts
STATUS_PLAYER     = "player_controlled"
STATUS_LEGION     = "legion_controlled"
STATUS_MAJ_PLAYER = "majority_player"
STATUS_MAJ_LEGION = "majority_legion"
STATUS_CONTESTED  = "contested"
STATUS_NEUTRAL    = "neutral"


def all_outer_addresses() -> list[str]:
    return OUTER_LABELS.copy()


def sub_addresses(parent: str) -> list[str]:
    return [f"{parent}-{p}" for p in SUB_POSITIONS]


def level_of(address: str) -> int:
    return address.count("-") + 1


def parent_of(address: str) -> Optional[str]:
    if "-" not in address:
        return None
    return address.rsplit("-", 1)[0]


def outer_of(address: str) -> str:
    """Return the level-1 outer hex for any address."""
    return address.split("-")[0]


def compute_status(child_statuses: list[str]) -> str:
    """
    Given a list of child controller values ('players','legion','neutral'),
    compute the aggregate status string for the parent hex.
    """
    total = len(child_statuses)
    if total == 0:
        return STATUS_NEUTRAL

    p = sum(1 for s in child_statuses if s == "players")
    l = sum(1 for s in child_statuses if s == "legion")
    n = total - p - l

    if p == total:
        return STATUS_PLAYER
    if l == total:
        return STATUS_LEGION
    if n > 0:
        # Still neutral hexes present
        return STATUS_NEUTRAL
    # No neutrals — all are either player or legion
    if p == l:
        return STATUS_CONTESTED
    if p > l:
        return STATUS_MAJ_PLAYER
    return STATUS_MAJ_LEGION


async def recompute_hex_statuses(conn, guild_id: int):
    """
    Recompute status for all level-2 and level-1 hexes bottom-up.
    Level-3 controller is set directly by combat.
    Level-2 status is derived from its level-3 children's controllers.
    Level-1 status is derived from its level-2 children's statuses mapped back to controllers.
    Hex A is always player_controlled.
    """
    # Level 2: derived from level-3 children
    mid_hexes = await conn.fetch(
        "SELECT address FROM hexes WHERE guild_id=$1 AND level=2", guild_id
    )
    for row in mid_hexes:
        addr = row["address"]
        children = await conn.fetch(
            "SELECT controller FROM hexes WHERE guild_id=$1 AND parent_address=$2",
            guild_id, addr
        )
        controllers = [c["controller"] for c in children]
        status = compute_status(controllers)
        # Also update controller of level-2 hex itself based on status
        ctrl = _status_to_controller(status)
        await conn.execute(
            "UPDATE hexes SET status=$1, controller=$2 WHERE guild_id=$3 AND address=$4",
            status, ctrl, guild_id, addr
        )

    # Level 1: derived from level-2 children's controllers
    outer_hexes = await conn.fetch(
        "SELECT address FROM hexes WHERE guild_id=$1 AND level=1", guild_id
    )
    for row in outer_hexes:
        addr = row["address"]
        if addr == SAFE_HUB:
            await conn.execute(
                "UPDATE hexes SET status=$1, controller=$2 WHERE guild_id=$3 AND address=$4",
                STATUS_PLAYER, "players", guild_id, addr
            )
            continue
        children = await conn.fetch(
            "SELECT controller FROM hexes WHERE guild_id=$1 AND parent_address=$2",
            guild_id, addr
        )
        controllers = [c["controller"] for c in children]
        status = compute_status(controllers)
        ctrl = _status_to_controller(status)
        await conn.execute(
            "UPDATE hexes SET status=$1, controller=$2 WHERE guild_id=$3 AND address=$4",
            status, ctrl, guild_id, addr
        )


def _status_to_controller(status: str) -> str:
    if status in (STATUS_PLAYER, STATUS_MAJ_PLAYER):
        return "players"
    if status in (STATUS_LEGION, STATUS_MAJ_LEGION):
        return "legion"
    return "neutral"


async def ensure_hexes(guild_id: int, conn):
    """Insert all hex rows for a guild if they don't exist yet."""
    for outer in OUTER_LABELS:
        ctrl = "players" if outer == SAFE_HUB else "neutral"
        status = STATUS_PLAYER if outer == SAFE_HUB else STATUS_NEUTRAL
        await conn.execute(
            """INSERT INTO hexes (guild_id, address, level, parent_address, controller, status)
               VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (guild_id, address) DO NOTHING""",
            guild_id, outer, 1, None, ctrl, status
        )
        for mid_pos in SUB_POSITIONS:
            mid = f"{outer}-{mid_pos}"
            mid_ctrl = "players" if outer == SAFE_HUB else "neutral"
            mid_status = STATUS_PLAYER if outer == SAFE_HUB else STATUS_NEUTRAL
            await conn.execute(
                """INSERT INTO hexes (guild_id, address, level, parent_address, controller, status)
                   VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (guild_id, address) DO NOTHING""",
                guild_id, mid, 2, outer, mid_ctrl, mid_status
            )
            for inner_pos in SUB_POSITIONS:
                inner = f"{mid}-{inner_pos}"
                await conn.execute(
                    """INSERT INTO hexes (guild_id, address, level, parent_address, controller, status)
                       VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (guild_id, address) DO NOTHING""",
                    guild_id, inner, 3, mid, mid_ctrl, mid_status
                )
