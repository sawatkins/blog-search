"""PostgreSQL crawl leases and a transactional search-index outbox.

Construction only connects; the migration CLI must explicitly call migrate().
Methods own their transactions and release connections before callers do HTTP/ES.
"""

import os
import math
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg2 import InterfaceError, OperationalError
from psycopg2.extensions import TRANSACTION_STATUS_UNKNOWN
from psycopg2.extras import Json, RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool

from scraper.fetching import MAX_COOLDOWN
from scraper.records import clean_title, content_fingerprint, nonarticle_reason, url_variant_key


# A scalar, indexed lookup avoids PostgreSQL building a hash of the entire
# ownership table to satisfy an EXISTS under OR for a tiny claim batch.
ACTIVE_JOB = """(job.manual OR COALESCE((
    SELECT true FROM crawl_job_sources origin JOIN crawl_sources source USING (feed_url)
    WHERE origin.job_id = job.id AND source.active LIMIT 1), false))"""


class SourceSyncError(ValueError):
    """An actionable source-list error containing no credentials or remote body."""


class Store:
    LEASE_SECONDS = 900
    MAX_ATTEMPTS = 8
    # Scheduled policy/configuration rechecks, not transient retry bursts.
    RECHECK_SECONDS = {'robots_denied': 30 * 86400, 'tls_error': 7 * 86400}

    def __init__(self, dsn=None):
        self._slots = BoundedSemaphore(4)
        self._pool = ThreadedConnectionPool(
            1, 4, dsn=dsn if dsn is not None else os.environ.get('DATABASE_URL') or '',
            connect_timeout=10,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Close idle connections after workers have finished; safe to repeat."""
        if not self._pool.closed:
            self._pool.closeall()

    @contextmanager
    def _transaction(self, *, crawl_write=False):
        with self._slots:
            connection = None
            try:
                # Probe before any application work. A suspended server may have
                # closed an idle socket. Never replay a transaction after yielding.
                for attempt in range(2):
                    connection = self._pool.getconn()
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute("SET LOCAL statement_timeout = '30s'")
                        break
                    except (InterfaceError, OperationalError):
                        self._pool.putconn(connection, close=True)
                        connection = None
                        if attempt:
                            raise
                with connection:
                    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                        if crawl_write:
                            # Serialize short queue writes, including rediscovery and
                            # page deduplication, across processes. Never hold for HTTP.
                            cursor.execute('SELECT pg_advisory_xact_lock(1937006962, 1)')
                        yield cursor
            finally:
                if connection is not None:
                    broken = bool(connection.closed) or connection.get_transaction_status() == TRANSACTION_STATUS_UNKNOWN
                    self._pool.putconn(connection, close=broken)

    def migrate(self):
        """Explicit, transactional schema installation; never called implicitly."""
        directory = Path(__file__).resolve().parent.parent / 'db'
        with self._transaction() as cursor:
            # Do not queue a schema lock behind a busy production transaction.
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            for name in ('schema.sql', 'crawler.sql'):
                cursor.execute((directory / name).read_text())

    def enqueue(self, jobs: list[dict]) -> int:
        """Return new jobs; preserve progress while accepting better discovery paths."""
        with self._transaction(crawl_write=True) as cursor:
            return self._enqueue(cursor, jobs)[0]

    def reconcile_sources(self, jobs, excluded=(), *, replace=True, allow_large_removal=False):
        """Atomically reconcile a validated Kagi snapshot without deleting content.

        Local pilot files use replace=False: they enroll only their selected feeds,
        never treat the unlisted rest of Kagi as removed. Known comic exclusions
        still apply to registered sources in either mode.
        """
        urls = sorted({item['url'] for item in jobs})
        excluded = sorted(set(excluded))
        if not urls or any(item['kind'] != 'feed' for item in jobs) or set(urls) & set(excluded):
            raise SourceSyncError('A nonempty, non-comic feed snapshot is required')
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute('SELECT feed_url FROM crawl_sources WHERE active')
            previous = {row['feed_url'] for row in cursor.fetchall()}
            removed = previous - set(urls) - set(excluded) if replace else set()
            # A truncated-but-valid download must not quietly disable most blogs.
            if previous and len(removed) > len(previous) * 0.2 and not allow_large_removal:
                raise SourceSyncError('Source list would remove over 20% of active feeds; inspect it before using --allow-large-removal')
            if replace:
                cursor.execute("""
                    UPDATE crawl_sources SET active = false, reason = 'removed', checked_at = NOW()
                    WHERE active AND NOT (feed_url = ANY(%s))
                """, (urls,))
            cursor.execute("""
                UPDATE crawl_sources SET active = false, reason = 'comic', checked_at = NOW()
                WHERE feed_url = ANY(%s)
            """, (excluded,))
            execute_values(cursor, """
                INSERT INTO crawl_sources (feed_url) VALUES %s
                ON CONFLICT (feed_url) DO UPDATE SET active = true, reason = NULL, checked_at = NOW()
            """, [(url,) for url in urls], page_size=500)
            managed = [{**item, 'manual': False, 'source_urls': [item['url']]} for item in jobs]
            inserted, _ = self._enqueue(cursor, managed)
            # Returning sources must be checked now, even if their old validator
            # would hide newly needed discovery. Existing failures remain visible.
            returning = sorted(set(urls) - previous)
            cursor.execute("""
                UPDATE crawl_jobs SET due_at = NOW(), etag = NULL, last_modified = NULL
                WHERE kind = 'feed' AND status <> 'running' AND url = ANY(%s)
            """, (returning,))
            return {'added_jobs': inserted, 'eligible_feeds': len(urls), 'removed': len(removed),
                    'comic_sources': len(previous & set(excluded))}

    @staticmethod
    def _enqueue(cursor, jobs, *, manual=True, source_urls=()):
        inserted, truncated = 0, False
        for start in range(0, len(jobs), 500):
            batch = {}
            ownership = {}
            for job in jobs[start:start + 500]:
                host = urlsplit(job['url']).hostname
                if not host:
                    raise ValueError('Crawl URLs must have a host')
                payload = job.get('payload', {})
                if not isinstance(payload, dict):
                    raise ValueError('Job payload must be a dict')
                root = payload.get('root') or job['url']
                if not isinstance(root, str):
                    raise ValueError('Job root must be a string')
                depth = payload.get('depth', 0)
                if type(depth) is not int or depth < 0:
                    raise ValueError('Job depth must be a nonnegative integer')
                scope = sha256(root.encode()).hexdigest() if job['kind'] in ('sitemap', 'archive', 'feed_page') else ''
                key = (job['kind'], job['url'], scope)
                ownership.setdefault(key, set()).update(job.get('source_urls', source_urls))
                independent = job.get('manual', manual)
                priority = job.get('priority', 0)
                if key in batch:
                    batch[key][4] = max(batch[key][4], priority)
                    batch[key][7] = batch[key][7] or independent
                    previous = batch[key][5].adapted
                    if previous.get('root') == payload.get('root') and depth < previous.get('depth', 0):
                        batch[key][5] = Json(payload)
                else:
                    batch[key] = [*key, host.rstrip('.'), priority, Json(payload),
                                  job.get('due_at'), independent]
            admitted = Store._admit_discovery(cursor, batch)
            truncated = truncated or len(admitted) < len(batch)
            if not admitted:
                continue
            rows = execute_values(cursor, """
                INSERT INTO crawl_jobs (kind, url, scope, host, priority, payload, due_at, manual)
                VALUES %s
                ON CONFLICT (kind, url, scope) DO UPDATE
                    SET priority = CASE WHEN crawl_jobs.status = 'pending'
                        THEN GREATEST(crawl_jobs.priority, EXCLUDED.priority) ELSE crawl_jobs.priority END,
                        manual = crawl_jobs.manual OR EXCLUDED.manual, updated_at = CURRENT_TIMESTAMP
                    WHERE (crawl_jobs.status = 'pending' AND crawl_jobs.priority < EXCLUDED.priority)
                        OR (EXCLUDED.manual AND NOT crawl_jobs.manual)
                RETURNING (xmax = 0) AS inserted
            """, admitted, template='(%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s)',
                page_size=500, fetch=True)
            inserted += sum(row['inserted'] for row in rows)
            sources = [(*row[:3], url) for row in admitted for url in sorted(ownership[tuple(row[:3])])]
            if sources:
                attached = execute_values(cursor, """
                    INSERT INTO crawl_job_sources (job_id, feed_url)
                    SELECT job.id, incoming.feed_url FROM (VALUES %s) AS incoming(kind, url, scope, feed_url)
                    JOIN crawl_jobs job ON (job.kind, job.url, job.scope) = (incoming.kind, incoming.url, incoming.scope)
                    ON CONFLICT DO NOTHING RETURNING job_id
                """, sources, page_size=500, fetch=True)
                if attached:
                    # A newly shared archive must rediscover descendants for the
                    # new owner; a 304 would otherwise retain only the old owner.
                    cursor.execute("""
                        UPDATE crawl_jobs SET etag = NULL, last_modified = NULL,
                            due_at = CASE WHEN attempts = 0 THEN NOW() ELSE due_at END,
                            status = CASE WHEN status = 'skipped' THEN 'pending' ELSE status END,
                            error = CASE WHEN status = 'skipped' THEN NULL ELSE error END
                        WHERE id = ANY(%s) AND kind <> 'feed' AND priority < 90
                            AND (status IN ('pending', 'running') OR
                                (status = 'skipped' AND error IN ('archive_listing', 'no_extractable_text')))
                    """, (sorted({row['job_id'] for row in attached}),))
        return inserted, truncated

    @staticmethod
    def _admit_discovery(cursor, batch):
        roots = {key: row[5].adapted.get('root') or row[1] for key, row in batch.items()
                 if row[0] != 'feed' and row[4] < 90
                 and (row[5].adapted.get('root') or row[0] in ('sitemap', 'archive', 'feed_page'))}
        if not roots:
            return list(batch.values())
        existing = execute_values(cursor, """
            SELECT job.id, job.kind, job.url, job.scope, job.payload, job.priority,
                job.status, job.error FROM crawl_jobs job
            JOIN (VALUES %s) AS incoming(kind, url, scope)
                ON (job.kind, job.url, job.scope) = (incoming.kind, incoming.url, incoming.scope)
        """, list(roots), page_size=500, fetch=True)
        Store._improve_discovery(cursor, existing, batch)
        existing = {(row['kind'], row['url'], row['scope']) for row in existing}
        new_roots = sorted({root for key, root in roots.items() if key not in existing})
        if not new_roots:
            return list(batch.values())
        execute_values(cursor, 'INSERT INTO crawl_sites (root) VALUES %s ON CONFLICT (root) DO NOTHING',
                       [(root,) for root in new_roots], page_size=500)
        cursor.execute('SELECT * FROM crawl_sites WHERE root = ANY(%s)', (new_roots,))
        sites = {row['root']: row for row in cursor.fetchall()}
        admitted = []
        for key, row in batch.items():
            if key in roots and key not in existing:
                site = sites[roots[key]]
                if site['discovered'] >= site['cap']:
                    site['limited'] = True
                    continue
                site['discovered'] += 1
            admitted.append(row)
        execute_values(cursor, """
            UPDATE crawl_sites site SET discovered = incoming.discovered, limited = incoming.limited
            FROM (VALUES %s) AS incoming(root, discovered, limited) WHERE site.root = incoming.root
        """, [(root, site['discovered'], site['limited']) for root, site in sites.items()], page_size=500)
        return admitted

    @staticmethod
    def _improve_discovery(cursor, existing, batch):
        """A shallower route must not inherit an earlier route's depth cutoff.

        Preserve failed jobs and retry backoff. A running job retains its lease;
        complete() notices its improved payload and schedules a full-body revisit.
        """
        updates = []
        for row in existing:
            payload = batch[(row['kind'], row['url'], row['scope'])][5].adapted
            old = row['payload']
            if (row['priority'] >= 90 or old.get('root') != payload.get('root')
                    or payload.get('depth', 0) >= old.get('depth', 0)):
                continue
            if row['status'] not in {'pending', 'running', 'skipped'}:
                continue
            if row['status'] == 'skipped' and row['error'] not in {'archive_listing', 'no_extractable_text'}:
                continue
            # A pending retry adopts the better route without accelerating retry.
            updates.append((row['id'], Json({**old, 'depth': payload.get('depth', 0)})))
        if updates:
            execute_values(cursor, """
                UPDATE crawl_jobs job SET payload = incoming.payload::jsonb,
                    etag = NULL, last_modified = NULL, updated_at = CURRENT_TIMESTAMP,
                    status = CASE WHEN job.status = 'running' THEN 'running' ELSE 'pending' END,
                    due_at = CASE WHEN job.attempts = 0 THEN CURRENT_TIMESTAMP ELSE job.due_at END,
                    error = CASE WHEN job.attempts = 0 THEN NULL ELSE job.error END
                FROM (VALUES %s) AS incoming(id, payload) WHERE job.id = incoming.id
            """, updates, page_size=500)

    def set_backfill_budget(self, root, budget):
        """Set a root's cap and replay historical discovery, keeping fresh jobs alone."""
        if not isinstance(root, str) or not root or type(budget) is not int or budget < 0:
            raise ValueError('A root and nonnegative integer budget are required')
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute("""
                INSERT INTO crawl_sites (root, cap, limited) VALUES (%s, %s, %s)
                ON CONFLICT (root) DO UPDATE SET cap = EXCLUDED.cap,
                    limited = crawl_sites.discovered >= EXCLUDED.cap
            """, (root, budget, budget == 0))
            cursor.execute("""
                UPDATE crawl_jobs SET status = 'pending', attempts = 0, error = NULL,
                    due_at = CURRENT_TIMESTAMP, etag = NULL, last_modified = NULL,
                    lease_until = NULL, lease_token = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE status NOT IN ('running', 'failed') AND (
                    (kind IN ('sitemap', 'archive', 'feed_page') AND scope = %s)
                    OR (kind = 'page' AND priority < 90 AND payload->>'root' = %s))
            """, (sha256(root.encode()).hexdigest(), root))
            return cursor.rowcount

    def claim(self, limit=8) -> list[dict]:
        if limit <= 0:
            return []
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute("""
                SET LOCAL jit = off;
                UPDATE crawl_jobs SET status = 'failed', lease_until = NULL,
                    lease_token = NULL, error = 'Lease expired after maximum attempts',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running' AND lease_until <= clock_timestamp()
                    AND kind <> 'feed' AND attempts >= %s
            """, (self.MAX_ATTEMPTS,))
            cursor.execute(f"""
                WITH eligible AS NOT MATERIALIZED (
                    SELECT * FROM crawl_jobs job
                    WHERE {ACTIVE_JOB} AND job.due_at <= statement_timestamp() AND (
                        job.status = 'pending'
                        OR (job.status = 'running' AND job.lease_until <= statement_timestamp())
                        OR (job.kind = 'feed' AND job.status = 'skipped'))
                ), selected AS (
                    SELECT job.id FROM eligible job
                    WHERE job.id = (
                        SELECT first.id FROM eligible first WHERE first.host = job.host
                        ORDER BY first.priority DESC, first.due_at, first.id LIMIT 1)
                    AND NOT EXISTS (
                        SELECT 1 FROM crawl_jobs active WHERE active.host = job.host
                            AND active.status = 'running' AND active.lease_until > statement_timestamp())
                    AND NOT EXISTS (
                        SELECT 1 FROM domains cooled WHERE cooled.domain = job.host
                            AND cooled.next_allowed_scrape > statement_timestamp())
                    ORDER BY job.priority DESC, job.due_at, job.id LIMIT %s
                ), claimed AS (
                    UPDATE crawl_jobs job SET status = 'running', attempts = attempts + 1,
                        lease_until = clock_timestamp() + %s * INTERVAL '1 second',
                        lease_token = %s, updated_at = CURRENT_TIMESTAMP
                    FROM selected WHERE job.id = selected.id RETURNING job.*
                )
                SELECT id, kind, url, host, priority, payload, attempts, lease_token, etag, last_modified, resolved_url
                FROM claimed ORDER BY priority DESC, due_at, id
            """, (limit, self.LEASE_SECONDS, str(uuid4())))
            return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _owned(cursor, job):
        cursor.execute("""
            SELECT * FROM crawl_jobs WHERE id = %s AND status = 'running'
                AND lease_token = %s AND lease_until > clock_timestamp() FOR UPDATE
        """, (job['id'], job['lease_token']))
        return cursor.fetchone()

    @staticmethod
    def _sources_for_completion(cursor, current):
        cursor.execute("""
            SELECT origin.feed_url FROM crawl_job_sources origin
            JOIN crawl_sources source USING (feed_url) WHERE origin.job_id = %s AND source.active
        """, (current['id'],))
        sources = [row['feed_url'] for row in cursor.fetchall()]
        if not current['manual'] and not sources:
            # Do not release the host early during sync; only the returning worker
            # or lease expiry ends the in-flight request. Its results are discarded.
            cursor.execute("""
                UPDATE crawl_jobs SET status = 'pending', lease_until = NULL, lease_token = NULL,
                    attempts = 0, error = NULL, updated_at = NOW() WHERE id = %s
            """, (current['id'],))
            return None
        return sources

    def complete(self, job, *, links=(), page=None, etag=None, last_modified=None,
                 not_modified=False, skip_reason=None, resolved_url=None):
        """Commit results atomically; return False for an expired/replaced lease.

        Success schedules feeds daily and other kinds after 30 days, resetting attempts.
        Validators survive 304s and skips; a 200 replaces even absent validators.
        Skipped feeds remain skipped until they become claimable the following day.
        """
        with self._transaction(crawl_write=True) as cursor:
            current = self._owned(cursor, job)
            if current is None:
                return False
            sources = self._sources_for_completion(cursor, current)
            if sources is None:
                return False
            _, truncated = self._enqueue(cursor, list(links), manual=current['manual'], source_urls=sources)
            if page is not None and (reason := nonarticle_reason(page['text'])):
                page, skip_reason = None, reason
            if page is not None:
                self._save_page(cursor, page, requested_url=current['url'])
            elif not_modified and current['kind'] == 'page':
                saved = self._find_page(cursor, current['resolved_url'] or current['url'])
                if saved:
                    cursor.execute('UPDATE pages SET scraped_on_date = CURRENT_TIMESTAMP WHERE id = %s', (saved['id'],))
            feed = current['kind'] == 'feed'
            status = 'skipped' if skip_reason is not None else 'pending'
            preserve = (not_modified or skip_reason is not None) and not truncated
            invalidated = not_modified and (
                (job.get('etag') is not None and current['etag'] is None)
                or (job.get('last_modified') is not None and current['last_modified'] is None))
            improved = invalidated or current['payload'].get('depth', 0) < (job.get('payload') or {}).get('depth', 0)
            if improved and skip_reason in (None, 'archive_listing', 'no_extractable_text'):
                status = 'pending'
            else:
                improved = False
            if truncated or improved:
                etag, last_modified = None, None
                preserve = False
            cursor.execute("""
                UPDATE crawl_jobs SET status = %s, due_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 day',
                    attempts = 0,
                    lease_until = NULL, lease_token = NULL, error = %s,
                    etag = CASE WHEN %s THEN etag ELSE %s END,
                    last_modified = CASE WHEN %s THEN last_modified ELSE %s END,
                    resolved_url = CASE WHEN %s THEN resolved_url ELSE %s END,
                    last_success_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE last_success_at END,
                    updated_at = CURRENT_TIMESTAMP WHERE id = %s
            """, (status, 0 if improved else (1 if feed else 30), skip_reason, preserve, etag, preserve, last_modified,
                  preserve, resolved_url or (page or {}).get('url') or current['url'],
                  skip_reason is None, current['id']))
            return True

    @staticmethod
    def _find_page(cursor, url):
        cursor.execute("""
            SELECT id, url FROM pages WHERE url = %s
            UNION ALL
            SELECT page.id, page.url FROM page_aliases alias JOIN pages page ON page.id = alias.page_id
            WHERE alias.url = %s LIMIT 1
        """, (url, url))
        return cursor.fetchone()

    @staticmethod
    def _alias(cursor, url, page_id):
        cursor.execute("""INSERT INTO page_aliases(url, page_id) VALUES (%s, %s)
            ON CONFLICT (url) DO UPDATE SET page_id = EXCLUDED.page_id""", (url, page_id))

    def _merge_pages(self, cursor, source_id, target_id):
        """Merge proven URL aliases; caller holds the crawl-write lock."""
        if source_id == target_id:
            return
        cursor.execute('SELECT url FROM pages WHERE id = %s', (source_id,))
        source = cursor.fetchone()
        if source is None:
            return
        cursor.execute('SELECT 1 FROM pages WHERE id = %s', (target_id,))
        if cursor.fetchone() is None:
            raise ValueError('Alias target does not exist')
        cursor.execute('UPDATE page_aliases SET page_id = %s WHERE page_id = %s', (target_id, source_id))
        self._delete_pages(cursor, [source_id])
        self._alias(cursor, source['url'], target_id)

    def _delete_pages(self, cursor, page_ids):
        cursor.execute('DELETE FROM pages WHERE id = ANY(%s) RETURNING id', (list(page_ids),))
        removed = [row['id'] for row in cursor.fetchall()]
        if removed:
            execute_values(cursor, """
                INSERT INTO crawl_index_jobs(page_id, operation) VALUES %s
                ON CONFLICT (page_id) DO UPDATE SET operation = 'delete',
                    revision = nextval('crawl_index_revision_seq'), due_at = CURRENT_TIMESTAMP,
                    attempts = 0, error = NULL
            """, [(page_id, 'delete') for page_id in removed], page_size=500)
        return removed

    def _save_page(self, cursor, page, *, requested_url=None):
        page = dict(page, title=clean_title(page.get('title'), page['url']),
                    fingerprint=content_fingerprint(page['text']))
        target = self._find_page(cursor, page['url'])
        if target is None:
            # Matching text only deduplicates HTTP/HTTPS/slash/tracking variants
            # of the same host/path, never unrelated URLs or different authors.
            variant = url_variant_key(page['url'])
            if variant:
                cursor.execute('SELECT id, url FROM pages WHERE fingerprint = %s ORDER BY id', (page['fingerprint'],))
                target = next((row for row in cursor.fetchall() if url_variant_key(row['url']) == variant), None)
        source = self._find_page(cursor, requested_url) if requested_url and requested_url != page['url'] else None
        if source and target and source['id'] != target['id']:
            self._merge_pages(cursor, source['id'], target['id'])
        target = target or source
        if target:
            # Keep the numeric article identity stable when its public URL moves.
            # Old URLs remain aliases even when the article text changes later.
            if target['url'] != page['url']:
                self._alias(cursor, target['url'], target['id'])
            cursor.execute('DELETE FROM page_aliases WHERE url = %s', (page['url'],))
            cursor.execute("""
                UPDATE pages SET title = %s, url = %s, fingerprint = %s, date = %s, text = %s,
                    scraped_on_date = CURRENT_TIMESTAMP WHERE id = %s
                    AND (title, url, fingerprint, date, text) IS DISTINCT FROM (%s, %s, %s, %s, %s)
                RETURNING id
            """, tuple(page[key] for key in ('title', 'url', 'fingerprint', 'date', 'text')) + (target['id'],)
                  + tuple(page[key] for key in ('title', 'url', 'fingerprint', 'date', 'text')))
            if cursor.fetchone():
                self._queue_index(cursor, target['id'])
            else:
                cursor.execute('UPDATE pages SET scraped_on_date = CURRENT_TIMESTAMP WHERE id = %s', (target['id'],))
            page_id = target['id']
            if requested_url and requested_url != page['url']:
                self._alias(cursor, requested_url, page_id)
            return page_id
        cursor.execute("""
            INSERT INTO pages (title, url, fingerprint, date, text)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title,
                fingerprint = EXCLUDED.fingerprint, date = EXCLUDED.date,
                text = EXCLUDED.text, scraped_on_date = CURRENT_TIMESTAMP
            WHERE (pages.title, pages.fingerprint, pages.date, pages.text)
                IS DISTINCT FROM (EXCLUDED.title, EXCLUDED.fingerprint, EXCLUDED.date, EXCLUDED.text)
            RETURNING id
        """, tuple(page[key] for key in ('title', 'url', 'fingerprint', 'date', 'text')))
        changed = cursor.fetchone()
        if changed:
            self._queue_index(cursor, changed['id'])
            if requested_url and requested_url != page['url']:
                self._alias(cursor, requested_url, changed['id'])
            return changed['id']
        cursor.execute('UPDATE pages SET scraped_on_date = CURRENT_TIMESTAMP WHERE url = %s RETURNING id',
                       (page['url'],))
        return cursor.fetchone()['id']

    @staticmethod
    def _queue_index(cursor, page_id=None):
        # Conflict revisions are allocated after the row lock; a queued insert's
        # default may have been allocated before a concurrent writer committed.
        cursor.execute("""
            INSERT INTO crawl_index_jobs (page_id) SELECT id FROM pages
        """ + ('WHERE id = %s ' if page_id is not None else '') + """
            ORDER BY id ON CONFLICT (page_id) DO UPDATE
                SET revision = nextval('crawl_index_revision_seq'), operation = 'index',
                    due_at = CURRENT_TIMESTAMP, attempts = 0, error = NULL
        """, (page_id,) if page_id is not None else ())
        return cursor.rowcount

    def index_batch(self, limit=100) -> list[dict]:
        with self._transaction() as cursor:
            cursor.execute("""
                SELECT queue.page_id, queue.revision, queue.operation, page.title, page.url, page.date,
                    page.text, page.scraped_on_date
                FROM crawl_index_jobs queue LEFT JOIN pages page ON page.id = queue.page_id
                WHERE queue.due_at <= CURRENT_TIMESTAMP ORDER BY queue.due_at, queue.page_id LIMIT %s
            """, (max(0, limit),))
            return [dict(row) for row in cursor.fetchall()]

    def index_done(self, page_id, revision):
        return self.finish_index([(page_id, revision)], []) == 1

    def index_failed(self, page_id, revision, error):
        return self.finish_index([], [(page_id, revision, error)]) == 1

    def finish_index(self, successes: list[tuple[int, int]], failures: list[tuple[int, int, str]]) -> int:
        """Atomically acknowledge/retry a batch; return matched rows, ignoring stale revisions."""
        if not successes and not failures:
            return 0
        with self._transaction() as cursor:
            count = 0
            for start in range(0, len(successes), 500):
                execute_values(cursor, """
                    DELETE FROM crawl_index_jobs queue USING (VALUES %s) AS finished(page_id, revision)
                    WHERE queue.page_id = finished.page_id AND queue.revision = finished.revision
                """, successes[start:start + 500], page_size=500)
                count += cursor.rowcount
            for start in range(0, len(failures), 500):
                execute_values(cursor, """
                    UPDATE crawl_index_jobs queue SET attempts = queue.attempts + 1, error = finished.error,
                        due_at = CURRENT_TIMESTAMP + LEAST(86400, 60 * power(2, LEAST(queue.attempts, 11)))
                            * INTERVAL '1 second'
                    FROM (VALUES %s) AS finished(page_id, revision, error)
                    WHERE queue.page_id = finished.page_id AND queue.revision = finished.revision
                """, [(page_id, revision, str(error)) for page_id, revision, error in failures[start:start + 500]],
                    page_size=500)
                count += cursor.rowcount
            return count

    def queue_reindex(self):
        """Enqueue all existing pages in SQL, bumping revisions without deleting ES data."""
        with self._transaction() as cursor:
            return self._queue_index(cursor)

    def host_cooldowns(self):
        """Restore rate-limit memory on restart, including redirect destinations."""
        with self._transaction() as cursor:
            cursor.execute("""SELECT domain, EXTRACT(EPOCH FROM next_allowed_scrape - CURRENT_TIMESTAMP) AS seconds
                FROM domains WHERE next_allowed_scrape > CURRENT_TIMESTAMP""")
            return {row['domain']: float(row['seconds']) for row in cursor.fetchall()}

    def defer(self, job, reason):
        """Return owned work after a run-level stop, without charging a URL retry."""
        with self._transaction(crawl_write=True) as cursor:
            current = self._owned(cursor, job)
            if current is None:
                return False
            if self._sources_for_completion(cursor, current) is None:
                return False
            cursor.execute("""
                UPDATE crawl_jobs SET status = 'pending', attempts = GREATEST(0, attempts - 1),
                    error = %s, lease_until = NULL, lease_token = NULL,
                    due_at = GREATEST(due_at, CURRENT_TIMESTAMP + INTERVAL '5 minutes'),
                    updated_at = CURRENT_TIMESTAMP WHERE id = %s
            """, (str(reason), current['id']))
            return True

    def fail(self, job, error, retryable=True, retry_after=None, *, status_code=None, host=None, failure_kind=None):
        """Record a failure without shortening server-requested cooling periods."""
        with self._transaction(crawl_write=True) as cursor:
            current = self._owned(cursor, job)
            if current is None:
                return False
            if self._sources_for_completion(cursor, current) is None:
                return False
            exhausted = current['attempts'] >= self.MAX_ATTEMPTS or not retryable
            feed = current['kind'] == 'feed'
            recheck = self.RECHECK_SECONDS.get(failure_kind)
            delay = min(86400, 60 * 2 ** min(max(current['attempts'] - 1, 0), 11))
            requested_delay = 0
            if retry_after is not None:
                seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
                if math.isfinite(seconds):
                    requested_delay = min(MAX_COOLDOWN, max(0, seconds))
                    delay = max(delay, requested_delay)
            if feed and exhausted:
                delay = max(delay, 30 * 86400 if status_code in (404, 410) else 86400)
            if recheck:
                delay = max(delay, recheck)
            if retry_after is not None or status_code == 429:
                # A URL's long recheck interval is NOT a host-wide rate limit.
                # Keep any actual cooldown (e.g. five minutes for broken robots),
                # without blocking working HTTP because HTTPS has a bad cert.
                cursor.execute("""
                    INSERT INTO domains (domain, next_allowed_scrape)
                    VALUES (%s, CURRENT_TIMESTAMP + %s * INTERVAL '1 second')
                    ON CONFLICT (domain) DO UPDATE SET next_allowed_scrape =
                        GREATEST(domains.next_allowed_scrape, EXCLUDED.next_allowed_scrape)
                """, (host or current['host'], max(60, requested_delay) if recheck else delay))
            cursor.execute("""
                UPDATE crawl_jobs SET status = %s, error = %s, lease_until = NULL, lease_token = NULL,
                    attempts = CASE WHEN %s THEN 0 ELSE attempts END,
                    due_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, ('failed' if exhausted and not feed and not recheck else 'pending', str(error),
                  bool(recheck), delay, current['id']))
            return True

    def status(self, *, root=None):
        """JSON-friendly counts, ready/expired jobs, outbox failures, and last success."""
        with self._transaction() as cursor:
            cursor.execute(f"""
                SELECT kind, status, COUNT(*) AS count, MAX(last_success_at) AS latest_success,
                    COUNT(*) FILTER (WHERE NOT {ACTIVE_JOB}) AS paused,
                    COUNT(*) FILTER (WHERE {ACTIVE_JOB} AND due_at <= CURRENT_TIMESTAMP AND
                        (status = 'pending' OR (status = 'running' AND lease_until <= CURRENT_TIMESTAMP)
                            OR (kind = 'feed' AND status = 'skipped'))) AS due
                FROM crawl_jobs job GROUP BY kind, status
            """)
            counts = {kind: dict.fromkeys(('pending', 'running', 'done', 'skipped', 'failed'), 0)
                      for kind in ('feed', 'feed_page', 'page', 'sitemap', 'archive')}
            due, paused, latest = 0, 0, None
            for row in cursor.fetchall():
                counts[row['kind']][row['status']] = row['count']
                due += row['due']
                paused += row['paused']
                if row['latest_success'] and (latest is None or row['latest_success'] > latest):
                    latest = row['latest_success']
            cursor.execute('SELECT COUNT(*) AS count, COUNT(*) FILTER (WHERE attempts > 0) AS failed FROM crawl_index_jobs')
            outbox = cursor.fetchone()
            cursor.execute("""
                SELECT COUNT(*) AS sites, COALESCE(SUM(cap), 0) AS cap,
                    COALESCE(SUM(discovered), 0) AS discovered,
                    COUNT(*) FILTER (WHERE limited) AS limited FROM crawl_sites
            """)
            backfill = dict(cursor.fetchone())
            cursor.execute('SELECT COUNT(*) FILTER (WHERE active) AS active, COUNT(*) FILTER (WHERE NOT active) AS inactive FROM crawl_sources')
            sources = dict(cursor.fetchone())
            result = {'counts': counts, 'due': due, 'source_paused_jobs': paused, 'sources': sources, 'outbox': outbox['count'],
                    'outbox_failed': outbox['failed'], 'latest_success': latest.isoformat() if latest else None,
                    'backfill_limited': backfill['limited'], 'backfill': backfill,
                    'backfill_note': 'Budgets are lifetime job caps, not daily allowances. Zero due jobs does not prove a complete history; depth limits and undiscoverable URLs also limit coverage.'}
            if root:
                cursor.execute('SELECT * FROM crawl_sites WHERE root = %s', (root,))
                site = cursor.fetchone()
                result['site'] = dict(site) if site else None
                if site:
                    cursor.execute(f"""
                        SELECT COUNT(*) FILTER (WHERE {ACTIVE_JOB} AND status = 'pending' AND due_at <= NOW()) AS due,
                            COUNT(*) FILTER (WHERE NOT {ACTIVE_JOB}) AS source_paused,
                            COUNT(*) FILTER (WHERE status = 'running') AS running,
                            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                            COUNT(*) FILTER (WHERE status = 'skipped') AS skipped,
                            COUNT(*) FILTER (WHERE last_success_at IS NOT NULL) AS visited
                        FROM crawl_jobs job WHERE payload->>'root' = %s AND kind <> 'feed' AND priority < 90
                    """, (root,))
                    result['site'].update(cursor.fetchone())
                    result['site']['coverage'] = 'budget_limited' if site['limited'] else 'not_proven_complete'
            return result

    def retry_failed(self):
        """Reset only terminal crawl failures and previously failed index entries."""
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute("""
                UPDATE crawl_jobs SET status = 'pending', attempts = 0, error = NULL,
                    due_at = CURRENT_TIMESTAMP, lease_until = NULL, lease_token = NULL,
                    updated_at = CURRENT_TIMESTAMP WHERE status = 'failed'
            """)
            count = cursor.rowcount
            cursor.execute("""
                UPDATE crawl_index_jobs SET attempts = 0, error = NULL, due_at = CURRENT_TIMESTAMP
                WHERE attempts > 0
            """)
            return count + cursor.rowcount

    def retry_skipped(self, *, limit=100):
        """Explicit, bounded retry after extraction changes; preserve policy skips."""
        if limit < 1:
            raise ValueError('Retry limit must be positive')
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute(f"""
                UPDATE crawl_jobs SET status = 'pending', attempts = 0, error = NULL,
                    due_at = CURRENT_TIMESTAMP, etag = NULL, last_modified = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id IN (
                    SELECT job.id FROM crawl_jobs job
                    WHERE kind = 'page' AND status = 'skipped' AND error = 'no_extractable_text'
                        AND {ACTIVE_JOB}
                    ORDER BY updated_at, id LIMIT %s
                )
            """, (limit,))
            return cursor.rowcount
