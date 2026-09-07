"""Bounded fetching, durable completion, and retryable indexing."""

import logging
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from gzip import BadGzipFile
from threading import Event, Lock
from urllib.parse import urljoin, urlsplit
from zlib import error as DecompressionError

from elasticsearch.helpers import streaming_bulk
from lxml.etree import XMLSyntaxError

from scraper.content import PageRejected, archive_jobs, extract_page, feed_jobs, is_archive_url, is_utility_url, job, sitemap_jobs
from scraper.fetching import FetchError

logger = logging.getLogger(__name__)


class CrawlPaused(RuntimeError):
    """Intake stopped for a suspected local outage; a new run is required."""


class Worker:
    def __init__(self, store, fetcher, *, workers=8, backfill_depth=5):
        self.store = store
        self.fetcher = fetcher
        self.workers = workers
        self.backfill_depth = backfill_depth
        self._halt = Event()
        self._health_lock = Lock()
        self._connection_failures = deque(maxlen=5)
        self._pause_reason = None

    def _pause(self, reason):
        with self._health_lock:
            if not self._halt.is_set():
                self._pause_reason = reason
                self._halt.set()

    def _connection_outcome(self, host=None, *, failed=False):
        with self._health_lock:
            if not failed:
                self._connection_failures.clear()
                return
            now = time.monotonic()
            self._connection_failures.append((now, host))
            recent = [host for when, host in self._connection_failures if now - when <= 300]
            if len(recent) == 5 and len(set(recent)) >= 3 and not self._halt.is_set():
                self._pause_reason = ('Crawler stopped: five connection/DNS failures across at least three hosts '
                                      'within five minutes, without a successful fetch or HTTP error response. '
                                      'Possible local network outage; check connectivity before starting a new run. '
                                      'Unclaimed jobs remain queued.')
                self._halt.set()

    def _defer(self, task):
        try:
            self.store.defer(task, self._pause_reason)
        except Exception as error:
            # If PostgreSQL itself is down, stop regardless. The existing lease
            # is the fallback recovery path; never conceal the failed release.
            logger.error('Could not release job %s (%s); its lease must expire before recovery',
                         task['id'], type(error).__name__)

    def process(self, task):
        if self._halt.is_set():
            self._defer(task)
            return
        try:
            payload = task.get("payload") or {}
            if task["kind"] == "page" and is_utility_url(task["url"], payload.get('scope_root') or payload.get("root")):
                self.store.complete(task, skip_reason="utility_page")
                return
            validator_url = task.get('resolved_url')
            # Legacy jobs have validators but no record of which redirect
            # destination issued them. Fetch once unconditionally to establish it.
            response = self.fetcher.fetch(task["url"], etag=task.get("etag") if validator_url else None,
                                          last_modified=task.get("last_modified") if validator_url else None,
                                          validator_url=validator_url)
            self._connection_outcome()
            if response.status == 304:
                self.store.complete(task, not_modified=True)
                return
            root = payload.get("root") or response.url
            discovery_root = payload.get('scope_root') or root
            depth = payload.get("depth", 0)
            kind = task["kind"]
            links, page, skip = [], None, None
            if kind in {"feed", "feed_page"}:
                try:
                    links, homepage = feed_jobs(response.body, response.url, depth=depth,
                                                root=root if kind == "feed_page" else None,
                                                historical=kind == "feed_page")
                except (ValueError, XMLSyntaxError) as error:
                    raise FetchError('Invalid feed content (' + type(error).__name__ + ')') from None
                if homepage and kind == "feed":
                    # Robots discovery is useful but must not discard valid feed
                    # entries if the site's robots endpoint is temporarily down.
                    try:
                        for url in self.fetcher.robots_sitemaps(homepage):
                            item = job("sitemap", url, priority=30, root=homepage)
                            if item:
                                links.append(item)
                    except FetchError:
                        logger.info("Robots sitemap discovery deferred for job %s", task["id"])
            elif kind == "sitemap":
                try:
                    links = sitemap_jobs(response.body, response.url, discovery_root, depth)
                except (ValueError, XMLSyntaxError, BadGzipFile, EOFError, DecompressionError) as error:
                    raise FetchError('Invalid sitemap content (' + type(error).__name__ + ')') from None
            elif kind == "archive":
                # Only a redirect of the actual root can move discovery scope.
                # A redirected post/CDN sitemap must not widen a whole blog.
                if task['url'] == discovery_root and response.url != discovery_root:
                    discovery_root = response.url
                    if discovery_root != root:
                        links.append(job('sitemap', urljoin(discovery_root, '/sitemap.xml'),
                                         root=root, priority=30))
                links.extend(archive_jobs(response.body, response.url, discovery_root, depth, self.backfill_depth, listing=True))
            elif kind == "page":
                if is_utility_url(response.url, discovery_root):
                    skip = "utility_page"
                elif response.content_type and not any(t in response.content_type.lower() for t in ("html", "text/plain")):
                    skip = "unsupported_content_type"
                else:
                    # Historical discovery must not stop at unfamiliar archive
                    # names such as /chronicle/ or year indexes. Follow in-scope
                    # HTML links even if the page itself has no article text.
                    # Feed entries stay on the fast path; depth and SQL budgets
                    # bound this traversal just like ordinary archive discovery.
                    historical_html = task.get("priority", 90) < 90 and (
                        not response.content_type or "html" in response.content_type.lower()
                    )
                    if historical_html and response.body:
                        links = archive_jobs(response.body, response.url, discovery_root, depth, self.backfill_depth)
                    if historical_html and is_archive_url(response.url, discovery_root):
                        skip = "archive_listing"
                    else:
                        page = extract_page(response.body, response.url, content_type=response.content_type,
                                            robots_header=getattr(response, 'robots_header', ''))
                        if not page:
                            skip = "no_extractable_text"
            else:
                raise ValueError("Unknown job kind")
            if kind not in {'feed', 'feed_page'} and discovery_root != root:
                for link in links:
                    # Keep the original lifetime budget/ownership identity while
                    # following the public homepage's demonstrated new scope.
                    link['payload'].update(root=root, scope_root=discovery_root)
            self.store.complete(task, links=links, page=page, etag=response.etag,
                                last_modified=response.last_modified, skip_reason=skip, resolved_url=response.url)
        except PageRejected as error:
            self.store.complete(task, skip_reason=str(error))
        except FetchError as error:
            if error.failure_kind == 'network_error':
                self._connection_outcome(error.host or urlsplit(task['url']).hostname, failed=True)
            elif error.status_code is not None:
                # Even a 404/503 proves that we reached an HTTP server.
                self._connection_outcome(error.host)
            if self._halt.is_set() and error.failure_kind == 'network_error':
                self._defer(task)
            else:
                self.store.fail(task, str(error), retryable=error.retryable,
                                retry_after=error.retry_after, status_code=error.status_code, host=error.host,
                                failure_kind=error.failure_kind)
        except Exception as error:
            self._pause(f'Crawler stopped: unexpected {type(error).__name__} in job {task["id"]}. '
                        'Check the crawler before starting a new run; unclaimed jobs remain queued.')
            self._defer(task)
            logger.error('Job %s stopped the crawler (%s)', task['id'], type(error).__name__)

    def run_batch(self, limit=None):
        """Compatibility helper for small, explicitly bounded pilots."""
        if self._halt.is_set():
            raise CrawlPaused(self._pause_reason)
        tasks = self.store.claim(min(limit if limit is not None else self.workers, self.workers))
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self.process, task) for task in tasks]
            for future in futures:
                future.result()
        if self._halt.is_set():
            raise CrawlPaused(self._pause_reason)
        return len(tasks)

    def run(self, *, max_jobs=None, stop=None, deadline=None, tick=None):
        """Refill free slots immediately; drain claimed work on stop or time limit.

        SQL leases still allow only one job per host. No unbounded in-memory queue
        is built: we claim at most the number of available worker slots.
        """
        if self._halt.is_set():
            raise CrawlPaused(self._pause_reason)
        claimed = 0
        pending = set()
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            while True:
                can_claim = (not self._halt.is_set() and not (stop and stop.is_set())
                             and (deadline is None or time.monotonic() < deadline))
                available = self.workers - len(pending)
                if max_jobs is not None:
                    available = min(available, max_jobs - claimed)
                if can_claim and available > 0:
                    tasks = self.store.claim(available)
                    claimed += len(tasks)
                    pending.update(executor.submit(self.process, task) for task in tasks)
                if tick:
                    tick()
                if not pending:
                    break
                completed, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in completed:
                    future.result()
        if self._halt.is_set():
            raise CrawlPaused(self._pause_reason)
        return claimed


def flush_index(store, client, index="pages", limit=100):
    """Acknowledge only successful writes of the version we actually sent."""
    rows = store.index_batch(limit)
    if not rows:
        return 0
    pending = {str(row["page_id"]): row for row in rows}
    actions = []
    for key, row in pending.items():
        # Server-side ordering also protects against a timed-out old request
        # finishing after its replacement. Same-revision retries are idempotent.
        action = {"_index": index, "_id": key, "_op_type": row.get('operation', 'index'),
                  "_version": row["revision"], "_version_type": "external_gte"}
        if action['_op_type'] == 'index':
            action['_source'] = {field: row[field] for field in ("title", "url", "date", "text", "scraped_on_date")}
        actions.append(action)
    successes, failures = [], []
    try:
        for ok, result in streaming_bulk(client, actions, chunk_size=limit, max_retries=0,
                                         raise_on_error=False, raise_on_exception=False):
            item = next(iter(result.values()))
            key = str(item["_id"])
            row = pending.pop(key)
            if ok or (row.get('operation') == 'delete' and item.get('status') == 404
                      and item.get('result') == 'not_found'):
                successes.append((row["page_id"], row["revision"]))
            else:
                failures.append((row["page_id"], row["revision"], "Elasticsearch HTTP " + str(item.get("status", "error"))))
    except Exception as error:
        for row in pending.values():
            failures.append((row["page_id"], row["revision"], type(error).__name__))
        logger.warning("Index batch deferred (%s)", type(error).__name__)
    # One database transaction per bulk response, not one round trip per page.
    # If this commit fails, all writes remain queued and are safe to send again.
    store.finish_index(successes, failures)
    return len(rows)
