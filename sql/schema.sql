-- Per-guild game configuration
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id BIGINT PRIMARY KEY,
    turn_interval_hours INT NOT NULL DEFAULT 8,
    last_turn_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    game_started BOOLEAN NOT NULL DEFAULT FALSE,
    gamemaster_role_id BIGINT DEFAULT NULL,  -- role that can control Legion manually
    report_channel_id BIGINT DEFAULT NULL    -- channel to post turn summaries
);

-- Hex map: level 1=outer, 2=mid, 3=inner
CREATE TABLE IF NOT EXISTS hexes (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    address TEXT NOT NULL,
    level INT NOT NULL,
    parent_address TEXT,
    controller TEXT NOT NULL DEFAULT 'neutral',
    -- derived status (recomputed each turn)
    status TEXT NOT NULL DEFAULT 'neutral',
    -- 'neutral','player_controlled','legion_controlled',
    -- 'majority_player','majority_legion','contested'
    UNIQUE(guild_id, address)
);

-- Player squadrons
CREATE TABLE IF NOT EXISTS squadrons (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    owner_name TEXT NOT NULL DEFAULT 'Handler',
    name TEXT NOT NULL,
    hex_address TEXT NOT NULL,
    deploy_hex TEXT DEFAULT NULL,           -- locked level-3 deploy point chosen at registration
    home_outer TEXT NOT NULL DEFAULT 'A',  -- outer hex this squadron belongs to
    in_transit BOOLEAN NOT NULL DEFAULT FALSE,
    transit_destination TEXT DEFAULT NULL,  -- level-3 address they're headed to
    transit_step INT NOT NULL DEFAULT 0,    -- 0=not in transit,1=going to A-C-C,2=going to dest
    attack INT NOT NULL DEFAULT 10,
    defense INT NOT NULL DEFAULT 10,
    speed INT NOT NULL DEFAULT 10,
    morale INT NOT NULL DEFAULT 10,
    supply INT NOT NULL DEFAULT 10,
    recon INT NOT NULL DEFAULT 10,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(guild_id, owner_id, name)
);

-- Legion units
CREATE TABLE IF NOT EXISTS legion_units (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    unit_type TEXT NOT NULL DEFAULT 'Grauwolf',
    hex_address TEXT NOT NULL,
    attack INT NOT NULL DEFAULT 10,
    defense INT NOT NULL DEFAULT 10,
    speed INT NOT NULL DEFAULT 10,
    morale INT NOT NULL DEFAULT 10,
    supply INT NOT NULL DEFAULT 10,
    recon INT NOT NULL DEFAULT 10,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    manually_moved BOOLEAN NOT NULL DEFAULT FALSE  -- set TRUE if GM moved this turn
);

-- Combat log
CREATE TABLE IF NOT EXISTS combat_log (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    turn_number INT NOT NULL,
    hex_address TEXT NOT NULL,
    attacker TEXT NOT NULL,
    defender TEXT NOT NULL,
    attacker_roll INT NOT NULL,
    defender_roll INT NOT NULL,
    outcome TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Turn counter
CREATE TABLE IF NOT EXISTS turn_history (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    turn_number INT NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Pending GM legion moves (cleared each turn)
CREATE TABLE IF NOT EXISTS legion_gm_moves (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    legion_unit_id INT NOT NULL,
    target_address TEXT NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(guild_id, legion_unit_id)  -- one queued move per unit per turn
);

CREATE INDEX IF NOT EXISTS idx_hexes_guild ON hexes(guild_id);
CREATE INDEX IF NOT EXISTS idx_squadrons_guild ON squadrons(guild_id);
CREATE INDEX IF NOT EXISTS idx_legion_guild ON legion_units(guild_id);
CREATE INDEX IF NOT EXISTS idx_combat_guild ON combat_log(guild_id);

-- Add new columns to existing tables if they don't exist (for existing deployments).
-- Each statement is wrapped in its own block so a genuine error surfaces rather
-- than being swallowed by a catch-all EXCEPTION WHEN OTHERS THEN NULL.
DO $$ BEGIN ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS gamemaster_role_id BIGINT DEFAULT NULL; END $$;
DO $$ BEGIN ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS report_channel_id BIGINT DEFAULT NULL; END $$;
DO $$ BEGIN ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS citadel_besieged BOOLEAN NOT NULL DEFAULT FALSE; END $$;
DO $$ BEGIN ALTER TABLE hexes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'neutral'; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS owner_name TEXT NOT NULL DEFAULT 'Handler'; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS deploy_hex TEXT DEFAULT NULL; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS home_outer TEXT NOT NULL DEFAULT 'A'; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS in_transit BOOLEAN NOT NULL DEFAULT FALSE; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS transit_destination TEXT DEFAULT NULL; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS transit_step INT NOT NULL DEFAULT 0; END $$;
DO $$ BEGIN ALTER TABLE legion_units ADD COLUMN IF NOT EXISTS manually_moved BOOLEAN NOT NULL DEFAULT FALSE; END $$;
DO $$ BEGIN ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS handler_role_id BIGINT DEFAULT NULL; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS last_scavenged_turn INT NOT NULL DEFAULT -1; END $$;

-- ─── FOB & Economy System ────────────────────────────────────────────────────

-- Per-player economy (raw materials + I.O.U.s)
CREATE TABLE IF NOT EXISTS player_economy (
    id            SERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    owner_id      BIGINT NOT NULL,
    raw_materials INT    NOT NULL DEFAULT 0,
    ious          INT    NOT NULL DEFAULT 0,
    UNIQUE(guild_id, owner_id)
);

-- Per-player FOB buildings (one row per building per player)
CREATE TABLE IF NOT EXISTS fob_buildings (
    id         SERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    owner_id   BIGINT NOT NULL,
    building   TEXT   NOT NULL,  -- 'command_bunker','barracks','armory','comms_tower','supply_depot','workshop'
    tier       INT    NOT NULL DEFAULT 0,  -- 0=not built, 1-5=upgrade level
    UNIQUE(guild_id, owner_id, building)
);

-- Stock market: per-guild stocks (GM-seeded, randomly fluctuating)
CREATE TABLE IF NOT EXISTS stocks (
    id           SERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    ticker       TEXT   NOT NULL,  -- e.g. 'MECH', 'FUEL', 'ARMS'
    name         TEXT   NOT NULL,
    price        INT    NOT NULL DEFAULT 100,
    trend        TEXT   NOT NULL DEFAULT 'stable',  -- 'bull','bear','stable','volatile'
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(guild_id, ticker)
);

-- Player stock holdings
CREATE TABLE IF NOT EXISTS stock_holdings (
    id       SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    ticker   TEXT   NOT NULL,
    shares   INT    NOT NULL DEFAULT 0,
    UNIQUE(guild_id, owner_id, ticker)
);

-- Citadel shop: GM-set processed material costs in I.O.U.s
-- (processed materials are just items you "buy" from citadel to spend on FOB)
CREATE TABLE IF NOT EXISTS citadel_shop (
    id         SERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    item       TEXT   NOT NULL,  -- 'processed_materials_small','processed_materials_large', etc.
    cost_ious  INT    NOT NULL DEFAULT 50,
    quantity   INT    NOT NULL DEFAULT 1,  -- how many processed materials you receive
    UNIQUE(guild_id, item)
);

-- ALTER TABLE additions for live deployments
DO $$ BEGIN ALTER TABLE player_economy ADD COLUMN IF NOT EXISTS raw_materials INT NOT NULL DEFAULT 0; END $$;
DO $$ BEGIN ALTER TABLE player_economy ADD COLUMN IF NOT EXISTS ious INT NOT NULL DEFAULT 0; END $$;
DO $$ BEGIN ALTER TABLE squadrons ADD COLUMN IF NOT EXISTS last_combat_turn INT NOT NULL DEFAULT -1; END $$;

DO $$ BEGIN ALTER TABLE player_economy ADD COLUMN IF NOT EXISTS processed_materials INT NOT NULL DEFAULT 0; END $$;


-- Stock price history (one row per ticker per turn for graphing)
CREATE TABLE IF NOT EXISTS stock_price_history (
    id         SERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    ticker     TEXT   NOT NULL,
    price      INT    NOT NULL,
    turn       INT    NOT NULL DEFAULT 0,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sph_guild_ticker ON stock_price_history(guild_id, ticker);

DO $$ BEGIN ALTER TABLE stock_price_history ADD COLUMN IF NOT EXISTS turn INT NOT NULL DEFAULT 0; END $$;
