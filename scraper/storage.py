"""PostgreSQL crawl leases and a transactional search-index outbox.

Construction only connects; the migration CLI must explicitly call migrate().
Methods own their transactions and release connections before callers do HTTP/ES.
"""

import os
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


class _DuplicatePage(Exception):
    """An existing URL changed to content already stored at a different URL."""


class Store:
    LEASE_SECONDS = 900
    MAX_ATTEMPTS = 8

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
            for name in ('schema.sql', 'crawler.sql'):
                cursor.execute((directory / name).read_text())

    def enqueue(self, jobs: list[dict]) -> int:
        """Return newly inserted jobs; rediscovery only raises pending priorities."""
        with self._transaction(crawl_write=True) as cursor:
            return self._enqueue(cursor, jobs)[0]

    @staticmethod
    def _enqueue(cursor, jobs):
        inserted, truncated = 0, False
        for start in range(0, len(jobs), 500):
            batch = {}
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
                scope = sha256(root.encode()).hexdigest() if job['kind'] in ('sitemap', 'archive') else ''
                key = (job['kind'], job['url'], scope)
                priority = job.get('priority', 0)
                if key in batch:
                    batch[key][4] = max(batch[key][4], priority)
                else:
                    batch[key] = [*key, host.rstrip('.'), priority, Json(payload),
                                  job.get('due_at')]
            admitted = Store._admit_discovery(cursor, batch)
            truncated = truncated or len(admitted) < len(batch)
            if not admitted:
                continue
            rows = execute_values(cursor, """
                INSERT INTO crawl_jobs (kind, url, scope, host, priority, payload, due_at)
                VALUES %s
                ON CONFLICT (kind, url, scope) DO UPDATE
                    SET priority = EXCLUDED.priority, updated_at = CURRENT_TIMESTAMP
                    WHERE crawl_jobs.status = 'pending'
                        AND crawl_jobs.priority < EXCLUDED.priority
                RETURNING (xmax = 0) AS inserted
            """, admitted, template='(%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()))',
                page_size=500, fetch=True)
            inserted += sum(row['inserted'] for row in rows)
        return inserted, truncated

    @staticmethod
    def _admit_discovery(cursor, batch):
        roots = {key: row[5].adapted.get('root') or row[1] for key, row in batch.items()
                 if row[0] != 'feed' and row[4] < 90
                 and (row[5].adapted.get('root') or row[0] in ('sitemap', 'archive'))}
        if not roots:
            return list(batch.values())
        existing = execute_values(cursor, """
            SELECT job.kind, job.url, job.scope FROM crawl_jobs job
            JOIN (VALUES %s) AS incoming(kind, url, scope)
                ON (job.kind, job.url, job.scope) = (incoming.kind, incoming.url, incoming.scope)
        """, list(roots), page_size=500, fetch=True)
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

    def set_backfill_budget(self, root, budget):
        """Set a root's cap, retaining usage; return non-running discovery jobs requeued."""
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
                WHERE kind IN ('sitemap', 'archive') AND scope = %s AND status <> 'running'
            """, (sha256(root.encode()).hexdigest(),))
            return cursor.rowcount

    def claim(self, limit=8) -> list[dict]:
        if limit <= 0:
            return []
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute("""
                UPDATE crawl_jobs SET status = 'failed', lease_until = NULL,
                    lease_token = NULL, error = 'Lease expired after maximum attempts',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running' AND lease_until <= clock_timestamp()
                    AND kind <> 'feed' AND attempts >= %s
            """, (self.MAX_ATTEMPTS,))
            cursor.execute("""
                WITH eligible AS NOT MATERIALIZED (
                    SELECT * FROM crawl_jobs job
                    WHERE job.due_at <= statement_timestamp() AND (
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
                SELECT id, kind, url, host, priority, payload, attempts, lease_token, etag, last_modified
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

    def complete(self, job, *, links=(), page=None, etag=None, last_modified=None,
                 not_modified=False, skip_reason=None):
        """Commit results atomically; return False for an expired/replaced lease.

        Success schedules feeds daily and other kinds after 30 days, resetting attempts.
        Validators survive 304s and skips; a 200 replaces even absent validators.
        Skipped feeds remain skipped until they become claimable the following day.
        """
        with self._transaction(crawl_write=True) as cursor:
            current = self._owned(cursor, job)
            if current is None:
                return False
            _, truncated = self._enqueue(cursor, list(links))
            if page is not None:
                try:
                    self._save_page(cursor, page)
                except _DuplicatePage as error:
                    skip_reason = str(error)
            elif not_modified and current['kind'] == 'page':
                cursor.execute('UPDATE pages SET scraped_on_date = CURRENT_TIMESTAMP WHERE url = %s',
                               (current['url'],))
            feed = current['kind'] == 'feed'
            status = 'skipped' if skip_reason is not None else 'pending'
            preserve = (not_modified or skip_reason is not None) and not truncated
            if truncated:
                etag, last_modified = None, None
            cursor.execute("""
                UPDATE crawl_jobs SET status = %s, due_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 day',
                    attempts = 0,
                    lease_until = NULL, lease_token = NULL, error = %s,
                    etag = CASE WHEN %s THEN etag ELSE %s END,
                    last_modified = CASE WHEN %s THEN last_modified ELSE %s END,
                    last_success_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE last_success_at END,
                    updated_at = CURRENT_TIMESTAMP WHERE id = %s
            """, (status, 1 if feed else 30, skip_reason, preserve, etag, preserve, last_modified,
                  skip_reason is None, current['id']))
            return True

    def _save_page(self, cursor, page):
        # Reuse a canonical page for a new alias. If a previously stored URL now
        # collides, flag the unresolved update instead of reporting fresh content.
        cursor.execute("""
            SELECT id, EXISTS (SELECT 1 FROM pages WHERE url = %s) AS url_exists
            FROM pages WHERE fingerprint = %s AND url IS DISTINCT FROM %s
        """, (page['url'], page['fingerprint'], page['url']))
        duplicate = cursor.fetchone()
        if duplicate:
            if duplicate['url_exists']:
                raise _DuplicatePage('duplicate_fingerprint: canonical page ' + str(duplicate['id']))
            cursor.execute('UPDATE pages SET scraped_on_date = CURRENT_TIMESTAMP WHERE id = %s',
                           (duplicate['id'],))
            return duplicate['id']
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
                SET revision = nextval('crawl_index_revision_seq'),
                    due_at = CURRENT_TIMESTAMP, attempts = 0, error = NULL
        """, (page_id,) if page_id is not None else ())
        return cursor.rowcount

    def index_batch(self, limit=100) -> list[dict]:
        with self._transaction() as cursor:
            cursor.execute("""
                SELECT queue.page_id, queue.revision, page.title, page.url, page.date,
                    page.text, page.scraped_on_date
                FROM crawl_index_jobs queue JOIN pages page ON page.id = queue.page_id
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

    def fail(self, job, error, retryable=True, retry_after=None, *, status_code=None):
        """Record failure; Retry-After accepts seconds or timedelta, capped at one day."""
        with self._transaction(crawl_write=True) as cursor:
            current = self._owned(cursor, job)
            if current is None:
                return False
            exhausted = current['attempts'] >= self.MAX_ATTEMPTS or not retryable
            feed = current['kind'] == 'feed'
            delay = min(86400, 60 * 2 ** min(max(current['attempts'] - 1, 0), 11))
            if retry_after is not None:
                seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
                delay = min(86400, max(delay, seconds))
            if feed and exhausted:
                delay = 86400
            if retry_after is not None or status_code == 429:
                cursor.execute("""
                    INSERT INTO domains (domain, next_allowed_scrape)
                    VALUES (%s, CURRENT_TIMESTAMP + %s * INTERVAL '1 second')
                    ON CONFLICT (domain) DO UPDATE SET next_allowed_scrape =
                        GREATEST(domains.next_allowed_scrape, EXCLUDED.next_allowed_scrape)
                """, (current['host'], delay))
            cursor.execute("""
                UPDATE crawl_jobs SET status = %s, error = %s, lease_until = NULL, lease_token = NULL,
                    due_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, ('failed' if exhausted and not feed else 'pending', str(error), delay, current['id']))
            return True

    def status(self):
        """JSON-friendly counts, ready/expired jobs, outbox failures, and last success."""
        with self._transaction() as cursor:
            cursor.execute("""
                SELECT kind, status, COUNT(*) AS count, MAX(last_success_at) AS latest_success,
                    COUNT(*) FILTER (WHERE due_at <= CURRENT_TIMESTAMP AND
                        (status = 'pending' OR (status = 'running' AND lease_until <= CURRENT_TIMESTAMP)
                            OR (kind = 'feed' AND status = 'skipped'))) AS due
                FROM crawl_jobs GROUP BY kind, status
            """)
            counts = {kind: dict.fromkeys(('pending', 'running', 'done', 'skipped', 'failed'), 0)
                      for kind in ('feed', 'page', 'sitemap', 'archive')}
            due, latest = 0, None
            for row in cursor.fetchall():
                counts[row['kind']][row['status']] = row['count']
                due += row['due']
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
            return {'counts': counts, 'due': due, 'outbox': outbox['count'],
                    'outbox_failed': outbox['failed'], 'latest_success': latest.isoformat() if latest else None,
                    'backfill_limited': backfill['limited'], 'backfill': backfill}

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

    def retry_skipped(self):
        """Explicitly reevaluate skipped pages without disturbing other work."""
        with self._transaction(crawl_write=True) as cursor:
            cursor.execute("""
                UPDATE crawl_jobs SET status = 'pending', attempts = 0, error = NULL,
                    due_at = CURRENT_TIMESTAMP, etag = NULL, last_modified = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE kind = 'page' AND status = 'skipped'
            """)
            return cursor.rowcount
