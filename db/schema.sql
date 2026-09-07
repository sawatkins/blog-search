-- Table for storing blog posts/pages
CREATE TABLE IF NOT EXISTS pages (
    id SERIAL PRIMARY KEY,
    title TEXT,
    url TEXT,
    fingerprint TEXT,
    date DATE,
    text TEXT,
    page_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(text, ''))) STORED,
    scraped_on_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Ensure unique indexes exist even if the table predated UNIQUE constraints
-- This allows INSERT ... ON CONFLICT (url) to work on older databases
CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_url_unique ON pages (url);
-- Identical words do not establish article identity (quotes, short posts,
-- syndication and shared boilerplate). URL aliases are tracked explicitly.
ALTER TABLE pages DROP CONSTRAINT IF EXISTS pages_fingerprint_key;
DROP INDEX IF EXISTS idx_pages_fingerprint_unique;
CREATE INDEX IF NOT EXISTS pages_fingerprint_idx ON pages (fingerprint);

-- Table for domain-based rate limiting
CREATE TABLE IF NOT EXISTS domains (
    domain TEXT PRIMARY KEY,
    next_allowed_scrape TIMESTAMP WITH TIME ZONE
);

-- Table for logging user queries
CREATE TABLE IF NOT EXISTS query_logs (
    id SERIAL PRIMARY KEY,
    query TEXT,
    ip_address TEXT,
    user_agent TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for full-text search
CREATE INDEX IF NOT EXISTS pages_tsv_idx ON pages USING gin(page_tsv);

-- Index for fast sorting by date (used by /latest endpoint)
-- Includes id for index-only scans on the subquery
CREATE INDEX IF NOT EXISTS pages_date_id_idx ON pages (date DESC NULLS LAST, id) WHERE date IS NOT NULL;

-- Legacy feeds/skipped_urls are no longer created. Their removal from an old
-- installation is an explicit backed-up maintenance action, not a migration side effect.
