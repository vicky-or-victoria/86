import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool, ensure_guild
from utils.hexmap import sub_addresses, level_of, parent_of, OUTER_LABELS, ensure_hexes
from utils.map_render import render_map_image


class HexMapView(discord.ui.View):
    def __init__(self, guild_id: int, address: str | None, render_level: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.address = address
        self.render_level = render_level

        if render_level < 3:
            labels = OUTER_LABELS if render_level == 1 else sub_addresses(address)
            for i, label in enumerate(labels):
                btn = discord.ui.Button(
                    label=f"▶ {label.split('-')[-1]}",
                    custom_id=f"hex_drill:{label}",
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
                custom_id="hex_back",
                style=discord.ButtonStyle.primary,
                row=2,
            )
            back_btn.callback = self._make_back_callback(back_target)
            self.add_item(back_btn)

    def _make_drill_callback(self, target_address: str):
        async def callback(interaction: discord.Interaction):
            await send_hex_view(interaction, self.guild_id, target_address, edit=True)
        return callback

    def _make_back_callback(self, target_address: str | None):
        async def callback(interaction: discord.Interaction):
            await send_hex_view(interaction, self.guild_id, target_address, edit=True)
        return callback


async def send_hex_view(
    interaction: discord.Interaction,
    guild_id: int,
    address: str | None,
    edit: bool = False,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
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
                # Level-3 view: units are directly here
                sq_rows = await conn.fetch(
                    "SELECT hex_address AS address, owner_name, in_transit FROM squadrons "
                    "WHERE guild_id=$1 AND is_active=TRUE AND hex_address = ANY($2::text[])",
                    guild_id, hex_addrs
                )
                squadrons = [dict(r) for r in sq_rows]

                lu_rows = await conn.fetch(
                    "SELECT hex_address AS address FROM legion_units "
                    "WHERE guild_id=$1 AND is_active=TRUE AND hex_address = ANY($2::text[])",
                    guild_id, hex_addrs
                )
                legion_units = [dict(r) for r in lu_rows]
            else:
                # Level-1 or level-2 view: aggregate presence from level-3 descendants.
                # For level-1: each hex in hex_addrs is an outer; aggregate all X-*-* under it.
                # For level-2: each hex in hex_addrs is a mid; aggregate all X-Y-* under it.
                # We query all level-3 squadrons/legion in the subtree and map them back
                # to their level-(render_level) ancestor.
                if render_level == 2:
                    # hex_addrs are mid-hex labels like ["B-1","B-2",...].
                    # Squadrons/legion live at level-3 whose parent_address is in hex_addrs.
                    sq_rows = await conn.fetch(
                        "SELECT s.hex_address, s.owner_name, s.in_transit "
                        "FROM squadrons s "
                        "JOIN hexes h ON h.guild_id = s.guild_id AND h.address = s.hex_address "
                        "WHERE s.guild_id=$1 AND s.is_active=TRUE AND h.level=3 "
                        "AND h.parent_address = ANY($2::text[])",
                        guild_id, hex_addrs
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
                        guild_id, hex_addrs
                    )
                    for r in lu_rows:
                        parent = r["hex_address"].rsplit("-", 1)[0]
                        legion_units.append({"address": parent})
                else:
                    # render_level == 1: outer hex view. hex_addrs are outer labels ["A","B",...].
                    # Aggregate all level-3 units whose outer matches.
                    sq_rows = await conn.fetch(
                        "SELECT s.hex_address, s.owner_name, s.in_transit "
                        "FROM squadrons s "
                        "JOIN hexes h ON h.guild_id = s.guild_id AND h.address = s.hex_address "
                        "WHERE s.guild_id=$1 AND s.is_active=TRUE AND h.level=3 "
                        "AND split_part(s.hex_address, '-', 1) = ANY($2::text[])",
                        guild_id, hex_addrs
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
                        guild_id, hex_addrs
                    )
                    for r in lu_rows:
                        outer = r["hex_address"].split("-")[0]
                        legion_units.append({"address": outer})

    hexes = [dict(r) for r in rows]

    if not hexes:
        content = "⚠️ No hex data found. Has the game been started? Use `/game_start`."
        if edit:
            await interaction.response.edit_message(content=content, view=discord.ui.View(), attachments=[])
        else:
            await interaction.response.send_message(content=content, ephemeral=True)
        return

    title = "Strategic Map — Outer Layer" if address is None else f"Hex {address} — Level {render_level}"
    map_file = render_map_image(hexes, title=title, parent=address,
                                squadrons=squadrons, legion_units=legion_units)

    embed = discord.Embed(title=title, color=discord.Color.from_rgb(52, 120, 210))
    embed.set_image(url="attachment://map.png")
    embed.set_footer(text="86 — Eighty Six  |  🔵 Player  🔴 Legion  🟣 Contested  ⬛ Neutral")

    view = HexMapView(guild_id=guild_id, address=address, render_level=render_level)

    if edit:
        await interaction.response.edit_message(embed=embed, attachments=[map_file], view=view)
    else:
        await interaction.response.send_message(embed=embed, file=map_file, view=view)


class MapCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="map", description="View the strategic hex map.")
    async def map_cmd(self, interaction: discord.Interaction):
        await ensure_guild(interaction.guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await ensure_hexes(interaction.guild_id, conn)
        await send_hex_view(interaction, interaction.guild_id, address=None)

    @app_commands.command(name="hex", description="Zoom into a specific hex by address (e.g. A, A-3, A-3-2).")
    @app_commands.describe(address="Hex address, e.g. 'A', 'A-3', or 'A-3-2'")
    async def hex_cmd(self, interaction: discord.Interaction, address: str):
        await ensure_guild(interaction.guild_id)
        address = address.strip().upper()
        await send_hex_view(interaction, interaction.guild_id, address=address)


async def setup(bot):
    await bot.add_cog(MapCog(bot))
