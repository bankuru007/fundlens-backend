-- ============================================
-- FundLens Database Schema
-- Run this in Supabase SQL Editor (one time)
-- ============================================

-- Stock intelligence table (aggregated signals per stock)
CREATE TABLE IF NOT EXISTS stock_intelligence (
    isin TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    nse_symbol TEXT,
    sector TEXT,
    signal TEXT CHECK (signal IN ('buy', 'sell', 'new', 'exit', 'hold')),
    total_amcs INTEGER DEFAULT 0,
    bought INTEGER DEFAULT 0,
    sold INTEGER DEFAULT 0,
    holding INTEGER DEFAULT 0,
    new_entry INTEGER DEFAULT 0,
    exited INTEGER DEFAULT 0,
    trend INTEGER[] DEFAULT '{}',
    data_month TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- AMC holdings per stock (detailed breakdown)
CREATE TABLE IF NOT EXISTS amc_holdings (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT REFERENCES stock_intelligence(isin),
    amc_name TEXT NOT NULL,
    action TEXT CHECK (action IN ('new_entry', 'buy', 'hold', 'sell', 'exit')),
    curr_qty BIGINT DEFAULT 0,
    prev_qty BIGINT DEFAULT 0,
    change_pct NUMERIC(8,2) DEFAULT 0,
    data_month TEXT,
    UNIQUE(isin, amc_name)
);

-- Raw holdings per AMC per month (for historical comparison)
CREATE TABLE IF NOT EXISTS amc_holdings_raw (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT,
    company TEXT NOT NULL,
    sector TEXT,
    amc_name TEXT NOT NULL,
    quantity BIGINT DEFAULT 0,
    value_lakhs NUMERIC(15,2) DEFAULT 0,
    data_month TEXT NOT NULL,
    UNIQUE(isin, amc_name, data_month)
);

-- Scrape audit log
CREATE TABLE IF NOT EXISTS scrape_meta (
    id BIGSERIAL PRIMARY KEY,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    data_month TEXT,
    amcs_scraped INTEGER DEFAULT 0,
    stocks_processed INTEGER DEFAULT 0,
    notes TEXT
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_stock_signal ON stock_intelligence(signal);
CREATE INDEX IF NOT EXISTS idx_stock_sector ON stock_intelligence(sector);
CREATE INDEX IF NOT EXISTS idx_stock_total_amcs ON stock_intelligence(total_amcs DESC);
CREATE INDEX IF NOT EXISTS idx_amc_holdings_isin ON amc_holdings(isin);
CREATE INDEX IF NOT EXISTS idx_amc_holdings_amc ON amc_holdings(amc_name);
CREATE INDEX IF NOT EXISTS idx_raw_month ON amc_holdings_raw(data_month);
CREATE INDEX IF NOT EXISTS idx_raw_amc ON amc_holdings_raw(amc_name);

-- Enable Row Level Security (public read)
ALTER TABLE stock_intelligence ENABLE ROW LEVEL SECURITY;
ALTER TABLE amc_holdings ENABLE ROW LEVEL SECURITY;
ALTER TABLE scrape_meta ENABLE ROW LEVEL SECURITY;

-- Allow public read access (your API handles writes with service key)
CREATE POLICY "Public read stock_intelligence"
    ON stock_intelligence FOR SELECT USING (true);

CREATE POLICY "Public read amc_holdings"
    ON amc_holdings FOR SELECT USING (true);

CREATE POLICY "Public read scrape_meta"
    ON scrape_meta FOR SELECT USING (true);

-- Service role can do everything (used by backend)
CREATE POLICY "Service role full access stock_intelligence"
    ON stock_intelligence FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access amc_holdings"
    ON amc_holdings FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access scrape_meta"
    ON scrape_meta FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role full access amc_holdings_raw"
    ON amc_holdings_raw FOR ALL USING (auth.role() = 'service_role');

-- ✅ Schema ready. Now run seed_db.py to populate initial data.
