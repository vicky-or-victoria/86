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

        # Load squadron presence for this level's hexes
        hex_addrs = [r["address"] for r in rows]
        squadrons = []
        legion_units = []
        if hex_addrs:
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
