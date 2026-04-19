import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import sub_addresses, level_of, parent_of, OUTER_LABELS, ensure_hexes
from utils.map_render import render_map_image


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_gm_or_admin(interaction: discord.Interaction, gm_role_id: int | None) -> bool:
    """Return True if the user is the server owner, an administrator, or holds the GM role."""
    member = interaction.user
    if interaction.guild.owner_id == member.id:
        return True
    if member.guild_permissions.administrator:
        return True
    if gm_role_id and any(r.id == gm_role_id for r in member.roles):
        return True
    return False


async def _fetch_map_data(conn, guild_id: int, address: str | None):
    """Fetch hex rows + unit presence for a given view level."""
    if address is None:
        rows = await conn.fetch(
            "SELECT address, controller, status FROM hexes WHERE guild_id=$1 AND level=1",
            guild_id,
        )
        render_level = 1
    else:
        rows = await conn.fetch(
            "SELECT address, controller, status FROM hexes "
            "WHERE guild_id=$1 AND parent_address=$2",
            guild_id, address,
        )
        render_level = level_of(address) + 1

    hex_addrs = [r["address"] for r in rows]
    squadrons = []
    legion_units = []

    if hex_addrs:
        if render_level == 3:
            sq_rows = await conn.fetch(
                "SELECT hex_address AS address, owner_name, in_transit FROM squadrons "
                "WHERE guild_id=$1 AND is_active=TRUE AND hex_address = ANY($2::text[])",
                guild_id, hex_addrs,
            )
            squadrons = [dict(r) for r in sq_rows]

            lu_rows = await conn.fetch(
                "SELECT hex_address AS address FROM legion_units "
                "WHERE guild_id=$1 AND is_active=TRUE AND hex_address = ANY($2::text[])",
                guild_id, hex_addrs,
            )
            legion_units = [dict(r) for r in lu_rows]
        elif render_level == 2:
            sq_rows = await conn.fetch(
                "SELECT s.hex_address, s.owner_name, s.in_transit "
                "FROM squadrons s "
                "JOIN hexes h ON h.guild_id = s.guild_id AND h.address = s.hex_address "
                "WHERE s.guild_id=$1 AND s.is_active=TRUE AND h.level=3 "
                "AND h.parent_address = ANY($2::text[])",
                guild_id, hex_addrs,
            )
            for r in sq_rows:
                parent = r["hex_address"].rsplit("-", 1)[0]
                squadrons.append({"address": parent, "owner_name": r["owner_name"],
                                  "in_transit": r["in_transit"]})

            lu_rows = await conn.fetch(
                "SELECT lu.hex_address "
                "FROM legion_units lu "
                "JOIN hexes h ON h.guild_id = lu.guild_id AND h.address = lu.hex_address "
                "WHERE lu.guild_id=$1 AND lu.is_active=TRUE AND h.level=3 "
                "AND h.parent_address = ANY($2::text[])",
                guild_id, hex_addrs,
            )
            for r in lu_rows:
                parent = r["hex_address"].rsplit("-", 1)[0]
                legion_units.append({"address": parent})
        else:
            sq_rows = await conn.fetch(
                "SELECT s.hex_address, s.owner_name, s.in_transit "
                "FROM squadrons s "
                "JOIN hexes h ON h.guild_id = s.guild_id AND h.address = s.hex_address "
                "WHERE s.guild_id=$1 AND s.is_active=TRUE AND h.level=3 "
                "AND split_part(s.hex_address, '-', 1) = ANY($2::text[])",
                guild_id, hex_addrs,
            )
            for r in sq_rows:
                outer = r["hex_address"].split("-")[0]
                squadrons.append({"address": outer, "owner_name": r["owner_name"],
                                  "in_transit": r["in_transit"]})

            lu_rows = await conn.fetch(
                "SELECT lu.hex_address "
                "FROM legion_units lu "
                "JOIN hexes h ON h.guild_id = lu.guild_id AND h.address = lu.hex_address "
                "WHERE lu.guild_id=$1 AND lu.is_active=TRUE AND h.level=3 "
                "AND split_part(lu.hex_address, '-', 1) = ANY($2::text[])",
                guild_id, hex_addrs,
            )
            for r in lu_rows:
                outer = r["hex_address"].split("-")[0]
                legion_units.append({"address": outer})

    return [dict(r) for r in rows], render_level, squadrons, legion_units


def _build_map_embed_and_file(hexes, render_level, squadrons, legion_units, address: str | None):
    title = "Strategic Map — Outer Layer" if address is None else f"Hex {address} — Level {render_level}"
    map_file = render_map_image(hexes, title=title, parent=address,
                                squadrons=squadrons, legion_units=legion_units)
    embed = discord.Embed(title=title, color=discord.Color.from_rgb(52, 120, 210))
    embed.set_image(url="attachment://map.png")
    embed.set_footer(text="86 — Eighty Six  |  🔵 Player  🔴 Legion  🟣 Contested  ⬛ Neutral")
    return embed, map_file


# ── Persistent map message tracking ──────────────────────────────────────────
# Stores the most recently posted persistent map message per guild so it can
# be edited automatically after each turn.
# Structure: { guild_id: {"channel_id": int, "message_id": int} }
_live_map_messages: dict[int, dict] = {}


async def auto_update_map(bot: commands.Bot, guild_id: int):
    """
    Called by the turn engine after each turn resolves.
    Edits the persistent map embed (if one exists for this guild) to reflect
    the latest state without any interaction context.
    """
    if guild_id not in _live_map_messages:
        return

    info = _live_map_messages[guild_id]
    guild = bot.get_guild(guild_id)
    if not guild:
        return

    channel = guild.get_channel(info["channel_id"])
    if not channel:
        return

    try:
        message = await channel.fetch_message(info["message_id"])
    except discord.NotFound:
        _live_map_messages.pop(guild_id, None)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        hexes, render_level, squadrons, legion_units = await _fetch_map_data(conn, guild_id, None)

    if not hexes:
        return

    embed, map_file = _build_map_embed_and_file(hexes, render_level, squadrons, legion_units, None)
    view = PublicMapView(guild_id=guild_id, address=None, render_level=1)
    await message.edit(embed=embed, attachments=[map_file], view=view)


# ── Views ─────────────────────────────────────────────────────────────────────

class PublicMapView(discord.ui.View):
    """
    Persistent view attached to the live map embed.
    Buttons are clickable by everyone but always respond ephemerally,
    so the live embed stays clean.
    """
    def __init__(self, guild_id: int, address: str | None, render_level: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.address = address
        self.render_level = render_level

        if render_level < 3:
            labels = OUTER_LABELS if render_level == 1 else sub_addresses(address)
            for i, label in enumerate(labels):
                btn = discord.ui.Button(
                    label=f"▶ {label.split('-')[-1]}",
                    custom_id=f"pubmap_drill:{guild_id}:{label}",
                    style=discord.ButtonStyle.secondary,
                    row=0 if i < 4 else 1,
                )
                btn.callback = self._make_drill_callback(label)
                self.add_item(btn)

        if render_level > 1:
            back_label = "⬅ Outer Map" if render_level == 2 else f"⬅ Back to {parent_of(address) or 'Map'}"
            back_target = None if render_level == 2 else parent_of(address)
            back_btn = discord.ui.Button(
                label=back_label,
                custom_id=f"pubmap_back:{guild_id}:{address}",
                style=discord.ButtonStyle.primary,
                row=2,
            )
            back_btn.callback = self._make_back_callback(back_target)
            self.add_item(back_btn)

    def _make_drill_callback(self, target_address: str):
        async def callback(interaction: discord.Interaction):
            await _send_ephemeral_hex_view(interaction, self.guild_id, target_address)
        return callback

    def _make_back_callback(self, target_address: str | None):
        async def callback(interaction: discord.Interaction):
            await _send_ephemeral_hex_view(interaction, self.guild_id, target_address)
        return callback


class EphemeralDrillView(discord.ui.View):
    """
    Ephemeral follow-up view shown to an individual player after clicking a map button.
    Drill-down and back buttons all stay ephemeral.
    """
    def __init__(self, guild_id: int, address: str | None, render_level: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.address = address
        self.render_level = render_level

        if render_level < 3:
            labels = OUTER_LABELS if render_level == 1 else sub_addresses(address)
            for i, label in enumerate(labels):
                btn = discord.ui.Button(
                    label=f"▶ {label.split('-')[-1]}",
                    custom_id=f"ephmap_drill:{guild_id}:{label}",
                    style=discord.ButtonStyle.secondary,
                    row=0 if i < 4 else 1,
                )
                btn.callback = self._make_drill_callback(label)
                self.add_item(btn)

        if render_level > 1:
            back_label = "⬅ Outer Map" if render_level == 2 else f"⬅ Back to {parent_of(address) or 'Map'}"
            back_target = None if render_level == 2 else parent_of(address)
            back_btn = discord.ui.Button(
                label=back_label,
                custom_id=f"ephmap_back:{guild_id}:{address or 'none'}",
                style=discord.ButtonStyle.primary,
                row=2,
            )
            back_btn.callback = self._make_back_callback(back_target)
            self.add_item(back_btn)

    def _make_drill_callback(self, target_address: str):
        async def callback(interaction: discord.Interaction):
            await _send_ephemeral_hex_view(interaction, self.guild_id, target_address, edit=True)
        return callback

    def _make_back_callback(self, target_address: str | None):
        async def callback(interaction: discord.Interaction):
            await _send_ephemeral_hex_view(interaction, self.guild_id, target_address, edit=True)
        return callback


async def _send_ephemeral_hex_view(
    interaction: discord.Interaction,
    guild_id: int,
    address: str | None,
    edit: bool = False,
):
    """Render a map view and send/edit it as an ephemeral message to the interacting user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        hexes, render_level, squadrons, legion_units = await _fetch_map_data(conn, guild_id, address)

    if not hexes:
        msg = "⚠️ No hex data found. Has the game been started? Use `/game_start`."
        if edit:
            await interaction.response.edit_message(content=msg, view=discord.ui.View(), attachments=[])
        else:
            await interaction.response.send_message(content=msg, ephemeral=True)
        return

    embed, map_file = _build_map_embed_and_file(hexes, render_level, squadrons, legion_units, address)
    view = EphemeralDrillView(guild_id=guild_id, address=address, render_level=render_level)

    if edit:
        await interaction.response.edit_message(embed=embed, attachments=[map_file], view=view)
    else:
        await interaction.response.send_message(embed=embed, file=map_file, view=view, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class MapCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="map", description="[GM] Post the live strategic map embed to this channel.")
    async def map_cmd(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()

        async with pool.acquire() as conn:
            config = await conn.fetchrow(
                "SELECT gamemaster_role_id FROM guild_config WHERE guild_id=$1",
                interaction.guild_id,
            )
            gm_role_id = config["gamemaster_role_id"] if config else None
            await ensure_hexes(interaction.guild_id, conn)

        if not _is_gm_or_admin(interaction, gm_role_id):
            await interaction.response.send_message(
                "❌ Only the server owner, administrators, or Gamemasters can post the live map.",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            hexes, render_level, squadrons, legion_units = await _fetch_map_data(
                conn, interaction.guild_id, None
            )

        embed, map_file = _build_map_embed_and_file(hexes, render_level, squadrons, legion_units, None)
        view = PublicMapView(guild_id=interaction.guild_id, address=None, render_level=1)

        await interaction.response.send_message(embed=embed, file=map_file, view=view)

        # Record this message so the turn engine can auto-update it.
        sent = await interaction.original_response()
        _live_map_messages[interaction.guild_id] = {
            "channel_id": interaction.channel_id,
            "message_id": sent.id,
        }

    @app_commands.command(name="hex", description="Zoom into a specific hex by address (e.g. A, A-3, A-3-2).")
    @app_commands.describe(address="Hex address, e.g. 'A', 'A-3', or 'A-3-2'")
    async def hex_cmd(self, interaction: discord.Interaction, address: str):
        await ensure_guild(interaction.guild_id)
        address = address.strip().upper()
        await _send_ephemeral_hex_view(interaction, interaction.guild_id, address=address)


async def setup(bot):
    await bot.add_cog(MapCog(bot))
