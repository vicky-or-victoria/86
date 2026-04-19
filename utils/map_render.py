"""
Renders the hex grid as a PNG image using Pillow.
Shows hex control status with 6 colors + aggregate unit count badges.

Unit display (scale-safe for 100+ players):
  Instead of one dot per player, each hex shows two compact badges:
    🔵  <N>  — total player squadrons present (blue badge, bottom-left)
    🔴  <N>  — total Legion units present     (red badge,  top-right)
  Badges only render if N > 0. This keeps the map legible at any player count.
"""

import io
import math
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
import discord

# ── Background & borders ──────────────────────────────────────────────────────
COLOR_BG        = (15, 17, 26)
COLOR_BORDER    = (200, 200, 220)
COLOR_TEXT      = (230, 230, 240)
COLOR_TITLE     = (180, 180, 200)
COLOR_LEGEND_BG = (25, 28, 40)

# ── Hex fill colors ───────────────────────────────────────────────────────────
COLOR_PLAYER        = (30,  90, 200)
COLOR_LEGION        = (180, 30,  30)
COLOR_MAJ_PLAYER    = (70, 130, 220)
COLOR_MAJ_LEGION    = (210, 80,  80)
COLOR_CONTESTED     = (130, 60, 160)
COLOR_NEUTRAL       = (50,  55,  70)

# ── Badge colors ──────────────────────────────────────────────────────────────
COLOR_BADGE_PLAYER  = (80,  160, 255)   # bright blue pill
COLOR_BADGE_LEGION  = (255, 80,  80)    # bright red pill
COLOR_BADGE_TEXT    = (10,  10,  10)    # dark text on badge
COLOR_BADGE_OUTLINE = (220, 220, 240)

STATUS_COLORS = {
    "player_controlled": COLOR_PLAYER,
    "legion_controlled": COLOR_LEGION,
    "majority_player":   COLOR_MAJ_PLAYER,
    "majority_legion":   COLOR_MAJ_LEGION,
    "contested":         COLOR_CONTESTED,
    "neutral":           COLOR_NEUTRAL,
    "players":           COLOR_PLAYER,
    "legion":            COLOR_LEGION,
}

STATUS_LABELS = {
    "player_controlled": "Player",
    "legion_controlled": "Legion",
    "majority_player":   "Maj. Player",
    "majority_legion":   "Maj. Legion",
    "contested":         "Contested",
    "neutral":           "Neutral",
    "players":           "Player",
    "legion":            "Legion",
}


def hex_corners(cx, cy, size):
    return [
        (cx + size * math.cos(math.radians(60 * i)),
         cy + size * math.sin(math.radians(60 * i)))
        for i in range(6)
    ]


def draw_hex(draw, cx, cy, size, fill, border, label, font, sublabel=None, small_font=None):
    corners = hex_corners(cx, cy, size)
    draw.polygon(corners, fill=fill)
    draw.polygon(corners, outline=border, width=2)

    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2, cy - th / 2 - (8 if sublabel else 0)),
              label, fill=COLOR_TEXT, font=font)

    if sublabel and small_font:
        sb = draw.textbbox((0, 0), sublabel, font=small_font)
        sw = sb[2] - sb[0]
        draw.text((cx - sw / 2, cy + th / 2 - 4),
                  sublabel, fill=(200, 200, 200), font=small_font)


def _draw_badge(draw, cx, cy, text: str, bg_color, font, x_offset: float, y_offset: float):
    """
    Draw a small rounded pill badge at (cx + x_offset, cy + y_offset).
    The pill auto-sizes to the text.
    """
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    pad_x, pad_y = 5, 3
    bx = cx + x_offset
    by = cy + y_offset
    # Pill background
    draw.rounded_rectangle(
        [(bx - tw / 2 - pad_x, by - th / 2 - pad_y),
         (bx + tw / 2 + pad_x, by + th / 2 + pad_y)],
        radius=5,
        fill=bg_color,
        outline=COLOR_BADGE_OUTLINE,
        width=1,
    )
    # Badge text
    draw.text((bx - tw / 2, by - th / 2), text, fill=COLOR_BADGE_TEXT, font=font)


def draw_unit_badges(draw, cx, cy, hex_size, player_count: int, legion_count: int, badge_font):
    """
    Draw aggregate unit count badges inside a hex.
      • Player badge (blue)  — bottom-left corner
      • Legion badge (red)   — top-right corner
    Badges only appear when count > 0.
    """
    offset_x = hex_size * 0.42
    offset_y = hex_size * 0.42

    if player_count > 0:
        label = f"🔵 {player_count}"
        _draw_badge(draw, cx, cy, label, COLOR_BADGE_PLAYER, badge_font,
                    x_offset=-offset_x * 0.5, y_offset=offset_y * 0.7)

    if legion_count > 0:
        label = f"🔴 {legion_count}"
        _draw_badge(draw, cx, cy, label, COLOR_BADGE_LEGION, badge_font,
                    x_offset=offset_x * 0.5, y_offset=-offset_y * 0.7)


def render_map_image(
    hexes: list[dict],
    title: str,
    parent: Optional[str],
    squadrons: list[dict] = None,    # [{address, owner_name, in_transit}]
    legion_units: list[dict] = None, # [{address}]
) -> discord.File:
    squadrons = squadrons or []
    legion_units = legion_units or []

    status_lookup = {h["address"]: h.get("status", h.get("controller", "neutral")) for h in hexes}

    # Aggregate counts per hex address
    player_count_by_hex: dict[str, int] = {}
    for s in squadrons:
        addr = s["address"]
        player_count_by_hex[addr] = player_count_by_hex.get(addr, 0) + 1

    legion_count_by_hex: dict[str, int] = {}
    for lu in legion_units:
        addr = lu["address"]
        legion_count_by_hex[addr] = legion_count_by_hex.get(addr, 0) + 1

    if parent is None:
        from utils.hexmap import OUTER_LABELS
        addresses = OUTER_LABELS
    else:
        from utils.hexmap import sub_addresses
        addresses = sub_addresses(parent)

    HEX_SIZE = 72
    W, H = 560, 520
    img = Image.new("RGB", (W, H), COLOR_BG)
    draw = ImageDraw.Draw(img)

    try:
        font       = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 17)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        badge_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11)
    except Exception:
        font = small_font = badge_font = ImageFont.load_default()

    cx_center, cy_center = W / 2, H / 2 - 15
    neighbour_dist = HEX_SIZE * math.sqrt(3)
    neighbour_angles = [0, 60, 120, 180, 240, 300]

    positions = [(cx_center, cy_center)]
    for angle_deg in neighbour_angles:
        angle = math.radians(angle_deg)
        positions.append((
            cx_center + neighbour_dist * math.cos(angle),
            cy_center + neighbour_dist * math.sin(angle),
        ))

    for i, addr in enumerate(addresses):
        if i >= len(positions):
            break
        hx, hy = positions[i]
        status = status_lookup.get(addr, "neutral")
        fill = STATUS_COLORS.get(status, COLOR_NEUTRAL)
        short = addr.split("-")[-1]
        status_label = STATUS_LABELS.get(status, "")
        draw_hex(draw, hx, hy, HEX_SIZE, fill, COLOR_BORDER, short, font,
                 sublabel=status_label, small_font=small_font)

        p_count = player_count_by_hex.get(addr, 0)
        l_count = legion_count_by_hex.get(addr, 0)
        if p_count > 0 or l_count > 0:
            draw_unit_badges(draw, hx, hy, HEX_SIZE, p_count, l_count, badge_font)

    # Title
    tb = draw.textbbox((0, 0), title, font=small_font)
    draw.text(((W - (tb[2] - tb[0])) / 2, 8), title, fill=COLOR_TITLE, font=small_font)

    # Legend
    legend_y = H - 44
    draw.rectangle([(0, legend_y - 6), (W, H)], fill=COLOR_LEGEND_BG)
    legend_items = [
        ("Player",     COLOR_PLAYER),
        ("Maj.Player", COLOR_MAJ_PLAYER),
        ("Legion",     COLOR_LEGION),
        ("Maj.Legion", COLOR_MAJ_LEGION),
        ("Contested",  COLOR_CONTESTED),
        ("Neutral",    COLOR_NEUTRAL),
    ]
    lx = 10
    for text, color in legend_items:
        draw.rectangle([(lx, legend_y), (lx + 12, legend_y + 12)], fill=color, outline=COLOR_BORDER)
        draw.text((lx + 15, legend_y), text, fill=COLOR_TEXT, font=badge_font)
        tb = draw.textbbox((0, 0), text, font=badge_font)
        lx += (tb[2] - tb[0]) + 32

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="map.png")
