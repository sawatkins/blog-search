-- Applied explicitly by Store.migrate(), after schema.sql.
-- Older installations used domains only to track feed checks. CREATE TABLE
-- IF NOT EXISTS in schema.sql cannot add the cooldown column to those tables.
ALTER TABLE domains ADD COLUMN IF NOT EXISTS next_allowed_scrape TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('feed', 'feed_page', 'page', 'sitemap', 'archive')),
    url TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'skipped', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    due_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_until TIMESTAMPTZ,
    lease_token UUID,
    etag TEXT,
    last_modified TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_success_at TIMESTAMPTZ,
    UNIQUE (kind, url, scope),
    CHECK ((status = 'running' AND lease_until IS NOT NULL AND lease_token IS NOT NULL)
        OR (status <> 'running' AND lease_until IS NULL AND lease_token IS NULL))
);

-- Historical feed pagination is separately budgeted, not a new daily source.
ALTER TABLE crawl_jobs DROP CONSTRAINT IF EXISTS crawl_jobs_kind_check;
ALTER TABLE crawl_jobs ADD CONSTRAINT crawl_jobs_kind_check
    CHECK (kind IN ('feed', 'feed_page', 'page', 'sitemap', 'archive'));

CREATE INDEX IF NOT EXISTS crawl_jobs_ready_idx
    ON crawl_jobs (priority DESC, due_at, id)
    WHERE status IN ('pending', 'running') OR (kind = 'feed' AND status = 'skipped');
CREATE INDEX IF NOT EXISTS crawl_jobs_host_lease_idx
    ON crawl_jobs (host, lease_until) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS crawl_jobs_host_ready_idx
    ON crawl_jobs (host, priority DESC, due_at, id)
    WHERE status IN ('pending', 'running') OR (kind = 'feed' AND status = 'skipped');
CREATE INDEX IF NOT EXISTS crawl_jobs_expired_idx
    ON crawl_jobs (lease_until) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS crawl_jobs_scope_idx
    ON crawl_jobs (scope) WHERE kind IN ('sitemap', 'archive');
CREATE INDEX IF NOT EXISTS crawl_jobs_feed_page_scope_idx
    ON crawl_jobs (scope) WHERE kind = 'feed_page';

-- Legacy/pilot jobs remain explicitly independent of the upstream registry.
ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS manual BOOLEAN NOT NULL DEFAULT true;
-- HTTP validators and saved content belong to the final URL, not a feed's
-- tracking/redirect link. Keep that link as the job identity so changes are seen.
ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS resolved_url TEXT;
CREATE TABLE IF NOT EXISTS crawl_sources (
    feed_url TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT true,
    reason TEXT CHECK (reason IN ('removed', 'comic')),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (active = (reason IS NULL))
);
-- A URL may be discovered by multiple feeds. Removing one must not stop another.
CREATE TABLE IF NOT EXISTS crawl_job_sources (
    job_id BIGINT NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    feed_url TEXT NOT NULL REFERENCES crawl_sources(feed_url),
    PRIMARY KEY (job_id, feed_url)
);
CREATE INDEX IF NOT EXISTS crawl_job_sources_feed_idx ON crawl_job_sources(feed_url, job_id);

CREATE TABLE IF NOT EXISTS crawl_sites (
    root TEXT PRIMARY KEY,
    cap INTEGER NOT NULL DEFAULT 10000 CHECK (cap >= 0),
    discovered INTEGER NOT NULL DEFAULT 0 CHECK (discovered >= 0),
    limited BOOLEAN NOT NULL DEFAULT false
);

-- A sequence prevents revision reuse after index_done() deletes an outbox row.
CREATE SEQUENCE IF NOT EXISTS crawl_index_revision_seq;
CREATE TABLE IF NOT EXISTS crawl_index_jobs (
    page_id INTEGER PRIMARY KEY REFERENCES pages (id) ON DELETE CASCADE,
    revision BIGINT NOT NULL DEFAULT nextval('crawl_index_revision_seq'),
    due_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error TEXT
);
CREATE INDEX IF NOT EXISTS crawl_index_jobs_due_idx ON crawl_index_jobs (due_at, page_id);

-- Deletions must survive removal of the page just as updates survive ES outages.
ALTER TABLE crawl_index_jobs DROP CONSTRAINT IF EXISTS crawl_index_jobs_page_id_fkey;
ALTER TABLE crawl_index_jobs ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT 'index'
    CHECK (operation IN ('index', 'delete'));

CREATE TABLE IF NOT EXISTS page_aliases (
    url TEXT PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS page_aliases_page_id_idx ON page_aliases(page_id);

-- Upgrade the old permanent/daily robots-denial policy without reviving dead
-- URLs or touching leases. Future failures already receive this interval in
-- Store.fail(); the due/updated condition makes repeated migrations a no-op.
UPDATE crawl_jobs SET status = 'pending', attempts = 0,
    due_at = GREATEST(due_at, CURRENT_TIMESTAMP + INTERVAL '30 days'),
    updated_at = CURRENT_TIMESTAMP
WHERE error = 'Blocked by robots policy' AND (
    status = 'failed' OR (status = 'pending' AND due_at < updated_at + INTERVAL '30 days')
);
