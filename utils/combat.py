"""
Combat engine for 86 bot.

Each combat is between a player squadron (or grouped players) and a Legion unit
on a contested hex. Stats influence dice roll modifiers:

  Attack  → bonus to attacker roll
  Defense → bonus to defender roll
  Speed   → bonus to initiative (who attacks first)
  Morale  → re-roll chance on failure (5% per morale point above 10)
  Supply  → penalty when below 5 (-2 to all rolls)
  Recon   → reveals enemy stat tier before rolling (info only, logged)
"""

import random
from dataclasses import dataclass


@dataclass
class CombatUnit:
    name: str
    side: str  # 'players' or 'legion'
    attack: int
    defense: int
    speed: int
    morale: int
    supply: int
    recon: int


@dataclass
class CombatResult:
    attacker: str
    defender: str
    attacker_roll: int
    defender_roll: int
    outcome: str  # 'attacker_wins', 'defender_wins', 'draw'
    narrative: str


def _roll(base: int, bonus: int, supply: int) -> int:
    supply_penalty = -2 if supply < 5 else 0
    raw = random.randint(1, 20)
    return max(1, raw + bonus + supply_penalty)


def _morale_reroll(unit: CombatUnit, roll: int) -> int:
    """If morale > 10, chance to reroll a bad result."""
    if unit.morale <= 10:
        return roll
    reroll_chance = (unit.morale - 10) * 0.05
    if roll <= 5 and random.random() < reroll_chance:
        new_roll = random.randint(1, 20)
        return max(roll, new_roll)
    return roll


def resolve_combat(attacker: CombatUnit, defender: CombatUnit) -> CombatResult:
    # Initiative: higher speed goes first (small bonus to attacker roll)
    speed_diff = attacker.speed - defender.speed
    initiative_bonus = max(-3, min(3, speed_diff // 3))

    atk_roll = _roll(attacker.attack, attacker.attack // 5 + initiative_bonus, attacker.supply)
    def_roll = _roll(defender.defense, defender.defense // 5, defender.supply)

    atk_roll = _morale_reroll(attacker, atk_roll)
    def_roll = _morale_reroll(defender, def_roll)

    if atk_roll > def_roll:
        outcome = "attacker_wins"
        narrative = (
            f"**{attacker.name}** overcomes **{defender.name}**! "
            f"(rolled {atk_roll} vs {def_roll})"
        )
    elif def_roll > atk_roll:
        outcome = "defender_wins"
        narrative = (
            f"**{defender.name}** holds the line against **{attacker.name}**! "
            f"(rolled {atk_roll} vs {def_roll})"
        )
    else:
        outcome = "draw"
        narrative = (
            f"**{attacker.name}** and **{defender.name}** fight to a standstill. "
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


def legion_unit_for_hex(address: str) -> CombatUnit:
    """Generate a Legion unit with slight variance based on hex depth."""
    level = address.count("-") + 1
    base = 8 + (level * 2)  # deeper hexes = stronger Legion
    variance = lambda: random.randint(-2, 2)
    return CombatUnit(
        name=f"Legion [{address}]",
        side="legion",
        attack=base + variance(),
        defense=base + variance(),
        speed=base + variance(),
        morale=base + variance(),
        supply=base + variance(),
        recon=base + variance(),
    )
