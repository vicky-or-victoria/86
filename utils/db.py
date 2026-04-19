import os
import asyncpg
from typing import Optional

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_schema():
    """Run schema.sql on startup."""
    pool = await get_pool()
    schema_path = os.path.join(os.path.dirname(__file__), "../sql/schema.sql")
    with open(schema_path, "r") as f:
        sql = f.read()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def ensure_guild(guild_id: int):
    """Create guild config row if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guild_config (guild_id)
            VALUES ($1)
            ON CONFLICT (guild_id) DO NOTHING
            """,
            guild_id,
        )
