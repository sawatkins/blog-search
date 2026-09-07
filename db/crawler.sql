-- Applied explicitly by Store.migrate(), after schema.sql.
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('feed', 'page', 'sitemap', 'archive')),
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
