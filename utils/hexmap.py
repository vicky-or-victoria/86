"""
Hex addressing scheme:
  Level 1 (outer):  A–G  (center=A, ring positions 1–6 = B–G)
  Level 2 (mid):    A-C (center), A-1 through A-6
  Level 3 (inner):  A-1-C, A-1-1 through A-1-6

Movement model:
  - Units live exclusively at level 3.
  - Intra-cluster move: any level-3 hex within the same level-2 parent. Free, instant.
  - Inter-cluster move: from an EDGE inner hex (positions 1–6, not C) to the adjacent
    level-2 cluster inside the same outer hex. Takes 1 turn transit.
  - Inter-outer move: must be at an edge cluster (mid positions 1–6, not C) of the
    current outer hex. Routes through A-C-C (safe hub center). Takes 2 turns.

Edge positions: 1–6 (not C). C is the center of a cluster and is landlocked.

Level-2 adjacency within the same outer hex (ring layout):
  Mid position → adjacent mid positions (same outer hex, wrapping ring)
  C  → {1,2,3,4,5,6}  (center touches all)
  1  → {C, 2, 6}
  2  → {C, 1, 3}
  3  → {C, 2, 4}
  4  → {C, 3, 5}
  5  → {C, 4, 6}
  6  → {C, 5, 1}
"""

from typing import Optional

OUTER_LABELS = ["A", "B", "C", "D", "E", "F", "G"]
SUB_POSITIONS = ["C", "1", "2", "3", "4", "5", "6"]
EDGE_POSITIONS = ["1", "2", "3", "4", "5", "6"]  # positions that border adjacent clusters

# Hex A is always player-controlled at game start
SAFE_HUB = "A"
SAFE_HUB_DEPLOY = "A-C-C"  # default deploy point inside Hex A

# Status labels derived from child hex counts
STATUS_PLAYER     = "player_controlled"
STATUS_LEGION     = "legion_controlled"
STATUS_MAJ_PLAYER = "majority_player"
STATUS_MAJ_LEGION = "majority_legion"
STATUS_CONTESTED  = "contested"
STATUS_NEUTRAL    = "neutral"

# Adjacency ring for level-2 mid positions within the same outer hex
_MID_RING_NEIGHBORS: dict[str, list[str]] = {
    "C": ["1", "2", "3", "4", "5", "6"],
    "1": ["C", "2", "6"],
    "2": ["C", "1", "3"],
    "3": ["C", "2", "4"],
    "4": ["C", "3", "5"],
    "5": ["C", "4", "6"],
    "6": ["C", "5", "1"],
}

# Which outer-hex ring position each mid-position faces outward toward
# (used for inter-outer adjacency: mid pos N of outer X faces outer Y)
# The 6 outer hexes B–G sit at ring positions 1–6 around center A.
# Outer hex at ring pos N faces back toward A with its own mid pos N.
_OUTER_RING_ORDER = ["B", "C", "D", "E", "F", "G"]  # positions 1–6 around A


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


def mid_of(address: str) -> Optional[str]:
    """Return the level-2 parent for a level-3 address, or None."""
    parts = address.split("-")
    if len(parts) < 3:
        return None
    return f"{parts[0]}-{parts[1]}"


def inner_pos(address: str) -> str:
    """Return the position label of a level-3 address (C, 1–6)."""
    return address.split("-")[-1]


def mid_pos(address: str) -> str:
    """Return the position label of a level-2 address (C, 1–6)."""
    parts = address.split("-")
    return parts[1] if len(parts) >= 2 else "C"


def is_edge_inner(address: str) -> bool:
    """True if this level-3 hex is on the edge of its cluster (pos 1–6, not C)."""
    return level_of(address) == 3 and inner_pos(address) in EDGE_POSITIONS


def is_edge_mid(address: str) -> bool:
    """True if this level-2 hex is on the edge of its outer hex (pos 1–6, not C)."""
    return level_of(address) == 2 and mid_pos(address) in EDGE_POSITIONS


def adjacent_inner_clusters(address: str) -> list[str]:
    """
    Given a level-3 address at an edge position, return the level-2 addresses of
    adjacent clusters within the same outer hex that this inner hex borders.
    Returns [] if at center (C) or not level-3.
    """
    if level_of(address) != 3:
        return []
    parts = address.split("-")
    outer = parts[0]
    my_mid_pos = parts[1]
    my_inner_pos = parts[2]

    if my_inner_pos not in EDGE_POSITIONS:
        return []  # C is landlocked

    # The inner edge positions that face toward an adjacent mid-cluster:
    # Within the cluster, inner pos N faces toward mid-neighbor N.
    neighbors = _MID_RING_NEIGHBORS.get(my_mid_pos, [])
    # The specific neighbor depends on inner_pos alignment.
    # Inner pos N of mid M borders mid neighbor N of M (within the same outer).
    # Also inner pos matching the mid's own ring direction can face outward (inter-outer).
    adjacent_mids = []
    if my_inner_pos in neighbors:
        adjacent_mids.append(f"{outer}-{my_inner_pos}")
    return adjacent_mids


def can_cross_to_outer(address: str) -> Optional[str]:
    """
    Given a level-3 address, return the adjacent outer hex label if this hex
    is at an inter-outer edge (i.e. edge inner pos of an edge mid cluster),
    or None if no inter-outer crossing is possible from here.

    Rule: a unit at X-N-N (mid pos N, inner pos N, where N is 1–6) is at the
    outward-facing corner and can initiate inter-outer transit.
    """
    if level_of(address) != 3:
        return None
    parts = address.split("-")
    outer = parts[0]
    my_mid_pos = parts[1]
    my_inner_pos = parts[2]

    # Must be at an edge mid AND edge inner, and inner pos must match mid pos
    # (the corner facing outward)
    if my_mid_pos == "C" or my_inner_pos == "C":
        return None
    if my_mid_pos != my_inner_pos:
        return None

    # Which outer hex does mid pos N face?
    try:
        ring_idx = int(my_mid_pos) - 1  # 1→0, 2→1, ..., 6→5
    except ValueError:
        return None

    if outer == SAFE_HUB:
        # From A, the outward corners face B–G
        return _OUTER_RING_ORDER[ring_idx]
    else:
        # From B–G, corner pos N faces back toward A (the hub)
        # and other positions face other outers — for now only hub routing supported
        return SAFE_HUB


def entry_hex_for_outer(destination_outer: str, coming_from_outer: str) -> str:
    """
    When a unit arrives at destination_outer coming from coming_from_outer,
    return the level-3 entry hex they land on.

    Units always enter at the facing corner of the destination:
    if coming from A into outer X (ring pos N), they enter X-N-N.
    If coming from outer X (ring pos N) into A, they enter A-N-N.
    """
    if coming_from_outer == SAFE_HUB:
        # Arriving at destination_outer from A
        # destination is at ring pos N — entry is destination-N-N
        try:
            ring_idx = _OUTER_RING_ORDER.index(destination_outer)
            pos = str(ring_idx + 1)
            return f"{destination_outer}-{pos}-{pos}"
        except ValueError:
            return f"{destination_outer}-C-C"
    else:
        # Arriving at A from coming_from_outer (ring pos N) — entry is A-N-N
        try:
            ring_idx = _OUTER_RING_ORDER.index(coming_from_outer)
            pos = str(ring_idx + 1)
            return f"{SAFE_HUB}-{pos}-{pos}"
        except ValueError:
            return SAFE_HUB_DEPLOY


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
