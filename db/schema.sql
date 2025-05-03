-- Table for storing blog posts/pages
CREATE TABLE IF NOT EXISTS pages (
    id SERIAL PRIMARY KEY,
    title TEXT,
    url TEXT UNIQUE,
    fingerprint TEXT UNIQUE,
    date DATE,
    text TEXT,
    page_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || text)) STORED,
    scraped_on_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Table for tracking RSS feeds
CREATE TABLE IF NOT EXISTS feeds (
    feed_url TEXT PRIMARY KEY,
    last_check_date DATE,
    is_active BOOLEAN DEFAULT true,
    date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Table for domain-based rate limiting
CREATE TABLE IF NOT EXISTS domains (
    domain TEXT PRIMARY KEY,
    scrape_error_count INTEGER DEFAULT 0,
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

-- Table for storing robots.txt data
CREATE TABLE IF NOT EXISTS robots_cache (
    domain TEXT PRIMARY KEY,
    content TEXT,
    last_fetched TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for full-text search
CREATE INDEX IF NOT EXISTS pages_tsv_idx ON pages USING gin(page_tsv);