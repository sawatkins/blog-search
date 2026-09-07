"""Bounded fetching, durable completion, and retryable indexing."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from elasticsearch.helpers import streaming_bulk

from scraper.content import archive_jobs, extract_page, feed_jobs, job, sitemap_jobs
from scraper.fetching import FetchError

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, fetcher, *, workers=8, backfill_depth=5):
        self.store = store
        self.fetcher = fetcher
        self.workers = workers
        self.backfill_depth = backfill_depth

    def process(self, task):
        try:
            response = self.fetcher.fetch(task["url"], etag=task.get("etag"), last_modified=task.get("last_modified"))
            if response.status == 304:
                self.store.complete(task, not_modified=True)
                return
            payload = task.get("payload") or {}
            root = payload.get("root") or response.url
            depth = payload.get("depth", 0)
            kind = task["kind"]
            links, page, skip = [], None, None
            if kind == "feed":
                links, homepage = feed_jobs(response.body, response.url)
                if homepage:
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
                links = sitemap_jobs(response.body, response.url, root, depth)
            elif kind == "archive":
                links = archive_jobs(response.body, response.url, root, depth, self.backfill_depth)
            elif kind == "page":
                if response.content_type and not any(t in response.content_type.lower() for t in ("html", "text/plain")):
                    skip = "unsupported_content_type"
                else:
                    page = extract_page(response.body, response.url)
                    if not page:
                        skip = "no_extractable_text"
            else:
                raise ValueError("Unknown job kind")
            self.store.complete(task, links=links, page=page, etag=response.etag,
                                last_modified=response.last_modified, skip_reason=skip)
        except FetchError as error:
            self.store.fail(task, str(error), retryable=error.retryable,
                            retry_after=error.retry_after, status_code=error.status_code)
        except Exception as error:
            # Errors remain visible and retryable; a saved lease also recovers if
            # the database itself is down and recording this failure fails.
            self.store.fail(task, type(error).__name__)
            logger.warning("Job %s failed (%s)", task["id"], type(error).__name__)

    def run_batch(self, limit=None):
        tasks = self.store.claim(min(limit or self.workers, self.workers))
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self.process, task) for task in tasks]
            for future in as_completed(futures):
                future.result()
        return len(tasks)


def flush_index(store, client, index="pages", limit=100):
    """Acknowledge only successful writes of the version we actually sent."""
    rows = store.index_batch(limit)
    if not rows:
        return 0
    pending = {str(row["page_id"]): row for row in rows}
    actions = []
    for key, row in pending.items():
        source = {field: row[field] for field in ("title", "url", "date", "text", "scraped_on_date")}
        actions.append({"_index": index, "_id": key, "_source": source})
    successes, failures = [], []
    try:
        for ok, result in streaming_bulk(client, actions, chunk_size=limit, max_retries=0,
                                         raise_on_error=False, raise_on_exception=False):
            item = next(iter(result.values()))
            key = str(item["_id"])
            row = pending.pop(key)
            if ok:
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
