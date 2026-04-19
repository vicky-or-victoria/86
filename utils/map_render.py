"""
Renders the hex grid as a PNG image using Pillow.
Shows hex control status with 6 colors + squadron presence indicators.
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
COLOR_PLAYER        = (30,  90, 200)   # solid blue
COLOR_LEGION        = (180, 30,  30)   # solid red
COLOR_MAJ_PLAYER    = (70, 130, 220)   # lighter blue
COLOR_MAJ_LEGION    = (210, 80,  80)   # lighter red
COLOR_CONTESTED     = (130, 60, 160)   # purple
COLOR_NEUTRAL       = (50,  55,  70)   # dark grey

# ── Presence circle colors ────────────────────────────────────────────────────
COLOR_PRESENCE_PLAYER = (120, 180, 255)  # light blue
COLOR_PRESENCE_LEGION = (255, 120, 120)  # light red
COLOR_PRESENCE_OUTLINE = (10, 10, 10)

STATUS_COLORS = {
    "player_controlled": COLOR_PLAYER,
    "legion_controlled": COLOR_LEGION,
    "majority_player":   COLOR_MAJ_PLAYER,
    "majority_legion":   COLOR_MAJ_LEGION,
    "contested":         COLOR_CONTESTED,
    "neutral":           COLOR_NEUTRAL,
    # fallback for raw controller values
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
    draw.text((cx - tw / 2, cy - th / 2 - (8 if sublabel else 0)), label, fill=COLOR_TEXT, font=font)

    if sublabel and small_font:
        sb = draw.textbbox((0, 0), sublabel, font=small_font)
        sw = sb[2] - sb[0]
        draw.text((cx - sw / 2, cy + th / 2 - 4), sublabel, fill=(200, 200, 200), font=small_font)


def draw_presence_circles(draw, cx, cy, hex_size, player_names: list[str], legion_count: int, tiny_font):
    """Draw small circles inside a hex showing unit presence."""
    r = 10
    offset = hex_size * 0.38

    # Player circles — bottom-left area
    for i, name in enumerate(player_names[:3]):  # max 3 shown
        px = cx - offset + i * (r * 2 + 3)
        py = cy + offset - r
        draw.ellipse([(px - r, py - r), (px + r, py + r)],
                     fill=COLOR_PRESENCE_PLAYER, outline=COLOR_PRESENCE_OUTLINE, width=1)
        # Username initial or truncated
        short = name[:2] if name else "?"
        tb = draw.textbbox((0, 0), short, font=tiny_font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((px - tw / 2, py - th / 2), short, fill=(10, 10, 40), font=tiny_font)

    if len(player_names) > 3:
        # +N overflow
        px = cx - offset + 3 * (r * 2 + 3)
        py = cy + offset - r
        draw.ellipse([(px - r, py - r), (px + r, py + r)],
                     fill=COLOR_PRESENCE_PLAYER, outline=COLOR_PRESENCE_OUTLINE, width=1)
        more = f"+{len(player_names) - 3}"
        tb = draw.textbbox((0, 0), more, font=tiny_font)
        draw.text((px - (tb[2] - tb[0]) / 2, py - (tb[3] - tb[1]) / 2), more, fill=(10, 10, 40), font=tiny_font)

    # Legion circles — top-right area
    for i in range(min(legion_count, 3)):
        px = cx + offset - i * (r * 2 + 3)
        py = cy - offset + r
        draw.ellipse([(px - r, py - r), (px + r, py + r)],
                     fill=COLOR_PRESENCE_LEGION, outline=COLOR_PRESENCE_OUTLINE, width=1)

    if legion_count > 3:
        px = cx + offset - 3 * (r * 2 + 3)
        py = cy - offset + r
        draw.ellipse([(px - r, py - r), (px + r, py + r)],
                     fill=COLOR_PRESENCE_LEGION, outline=COLOR_PRESENCE_OUTLINE, width=1)
        more = f"+{legion_count - 3}"
        tb = draw.textbbox((0, 0), more, font=tiny_font)
        draw.text((px - (tb[2] - tb[0]) / 2, py - (tb[3] - tb[1]) / 2), more, fill=(40, 10, 10), font=tiny_font)


def render_map_image(
    hexes: list[dict],
    title: str,
    parent: Optional[str],
    squadrons: list[dict] = None,   # [{address, owner_name, in_transit}]
    legion_units: list[dict] = None, # [{address}]
) -> discord.File:
    squadrons = squadrons or []
    legion_units = legion_units or []

    # Build lookups
    status_lookup = {h["address"]: h.get("status", h.get("controller", "neutral")) for h in hexes}

    # Group presence by hex
    player_by_hex: dict[str, list[str]] = {}
    for s in squadrons:
        addr = s["address"]
        name = s.get("owner_name", "?")
        if s.get("in_transit"):
            name = f"✈{name}"
        player_by_hex.setdefault(addr, []).append(name)

    legion_by_hex: dict[str, int] = {}
    for lu in legion_units:
        addr = lu["address"]
        legion_by_hex[addr] = legion_by_hex.get(addr, 0) + 1

    # Address order
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
        tiny_font  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 9)
    except Exception:
        font = small_font = tiny_font = ImageFont.load_default()

    cx, cy = W / 2, H / 2 - 15
    neighbour_dist = HEX_SIZE * math.sqrt(3)
    neighbour_angles = [0, 60, 120, 180, 240, 300]

    positions = [(cx, cy)]
    for angle_deg in neighbour_angles:
        angle = math.radians(angle_deg)
        positions.append((
            cx + neighbour_dist * math.cos(angle),
            cy + neighbour_dist * math.sin(angle),
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

        # Presence circles
        p_names = player_by_hex.get(addr, [])
        l_count = legion_by_hex.get(addr, 0)
        if p_names or l_count:
            draw_presence_circles(draw, hx, hy, HEX_SIZE, p_names, l_count, tiny_font)

    # Title
    tb = draw.textbbox((0, 0), title, font=small_font)
    draw.text(((W - (tb[2] - tb[0])) / 2, 8), title, fill=COLOR_TITLE, font=small_font)

    # Legend
    legend_y = H - 44
    draw.rectangle([(0, legend_y - 6), (W, H)], fill=COLOR_LEGEND_BG)
    legend_items = [
        ("Player",      COLOR_PLAYER),
        ("Maj.Player",  COLOR_MAJ_PLAYER),
        ("Legion",      COLOR_LEGION),
        ("Maj.Legion",  COLOR_MAJ_LEGION),
        ("Contested",   COLOR_CONTESTED),
        ("Neutral",     COLOR_NEUTRAL),
    ]
    lx = 10
    for text, color in legend_items:
        draw.rectangle([(lx, legend_y), (lx + 12, legend_y + 12)], fill=color, outline=COLOR_BORDER)
        draw.text((lx + 15, legend_y), text, fill=COLOR_TEXT, font=tiny_font)
        tb = draw.textbbox((0, 0), text, font=tiny_font)
        lx += (tb[2] - tb[0]) + 32

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="map.png")
