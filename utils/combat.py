"""
Combat engine for 86 bot.

Each combat is between a player squadron (or grouped players) and a Legion unit
on a contested hex. Stats influence dice roll modifiers:

  Attack  → bonus to attacker roll
  Defense → bonus to defender roll
  Speed   → bonus to initiative (who attacks first)
  Morale  → re-roll chance on failure (5% per morale point above 10)
  Supply  → penalty when below 5 (-2 to all rolls)
  Recon   → two effects:
              1. If player recon > Legion recon: +1 to attack roll (intel advantage).
              2. Shepherd and Dinosauria gain +3 defense against squadrons with
                 recon < 8 (ambush/swarm). Recon >= 8 fully negates this bonus.
"""

import random
from dataclasses import dataclass, field

# Legion unit types that gain an ambush bonus against low-recon squadrons
_AMBUSH_TYPES           = {"Shepherd", "Dinosauria"}
_RECON_AMBUSH_THRESHOLD = 8   # player recon below this = vulnerable
_RECON_AMBUSH_BONUS     = 3   # added to Legion defense when ambush applies
_RECON_INTEL_BONUS      = 1   # added to player attack when recon > legion recon


@dataclass
class CombatUnit:
    name: str
    side: str        # 'players' or 'legion'
    attack: int
    defense: int
    speed: int
    morale: int
    supply: int
    recon: int
    unit_type: str = field(default="")  # Legion unit type (Grauwolf, Shepherd, …); empty for players


@dataclass
class CombatResult:
    attacker: str
    defender: str
    attacker_roll: int
    defender_roll: int
    outcome: str      # 'attacker_wins', 'defender_wins', 'draw'
    narrative: str


# ── Internal helpers ──────────────────────────────────────────────────────────

def _roll(bonus: int, supply: int) -> int:
    supply_penalty = -2 if supply < 5 else 0
    raw = random.randint(1, 20)
    return max(1, raw + bonus + supply_penalty)


def _morale_reroll(unit: CombatUnit, roll: int) -> int:
    """If morale > 10, chance to reroll a bad result (always keep the better)."""
    if unit.morale <= 10:
        return roll
    reroll_chance = (unit.morale - 10) * 0.05
    if roll <= 5 and random.random() < reroll_chance:
        return max(roll, random.randint(1, 20))
    return roll


def _recon_modifiers(attacker: CombatUnit, defender: CombatUnit) -> tuple[int, int]:
    """
    Returns (attacker_bonus, defender_bonus) derived from recon interaction.

    Only applied when the attacker is a player squadron. Two independent checks:
      - Intel edge: player out-recons the Legion unit → +1 attack.
      - Ambush:     Legion unit is an ambush type AND player recon < threshold → +3 defense.
    Both can apply simultaneously.
    """
    if attacker.side != "players":
        return 0, 0

    atk_bonus = _RECON_INTEL_BONUS if attacker.recon > defender.recon else 0
    def_bonus = (
        _RECON_AMBUSH_BONUS
        if defender.unit_type in _AMBUSH_TYPES and attacker.recon < _RECON_AMBUSH_THRESHOLD
        else 0
    )
    return atk_bonus, def_bonus


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_combat(attacker: CombatUnit, defender: CombatUnit) -> CombatResult:
    # Initiative: higher speed goes first (small bonus to attacker roll, capped ±3)
    speed_diff       = attacker.speed - defender.speed
    initiative_bonus = max(-3, min(3, speed_diff // 3))

    recon_atk, recon_def = _recon_modifiers(attacker, defender)

    atk_bonus = attacker.attack // 5 + initiative_bonus + recon_atk
    def_bonus = defender.defense // 5 + recon_def

    atk_roll = _roll(atk_bonus, attacker.supply)
    def_roll = _roll(def_bonus, defender.supply)

    atk_roll = _morale_reroll(attacker, atk_roll)
    def_roll = _morale_reroll(defender, def_roll)

    # Build recon flavour text for the narrative
    recon_notes = []
    if recon_atk:
        recon_notes.append("intel advantage")
    if recon_def:
        recon_notes.append(f"{defender.unit_type} ambush")
    recon_str = f" *[{', '.join(recon_notes)}]*" if recon_notes else ""

    if atk_roll > def_roll:
        outcome = "attacker_wins"
        narrative = (
            f"**{attacker.name}** overcomes **{defender.name}**!{recon_str} "
            f"(rolled {atk_roll} vs {def_roll})"
        )
    elif def_roll > atk_roll:
        outcome = "defender_wins"
        narrative = (
            f"**{defender.name}** holds the line against **{attacker.name}**!{recon_str} "
            f"(rolled {atk_roll} vs {def_roll})"
        )
    else:
        outcome = "draw"
        narrative = (
            f"**{attacker.name}** and **{defender.name}** fight to a standstill.{recon_str} "
            f"(both rolled {atk_roll})"
        )

    return CombatResult(
        attacker=attacker.name,
        defender=defender.name,
        attacker_roll=atk_roll,
        defender_roll=def_roll,
        outcome=outcome,
        narrative=narrative,
    )
