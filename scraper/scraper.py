"""Small CLI for the crawler. Importing this module never starts services."""

import argparse
import fcntl
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin

# Support both python -m scraper.scraper and the old script entry point.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from elasticsearch import Elasticsearch

from scraper.content import SMALLWEB_URL, job, source_jobs
from scraper.fetching import Fetcher, normalize_url
from scraper.storage import Store
from scraper.worker import Worker, flush_index

logger = logging.getLogger(__name__)
PROJECT = Path(__file__).resolve().parents[1]


@contextmanager
def process_lock():
    """This deployment has one host; a file lock prevents overlapping workers."""
    directory = Path(os.getenv("CRAWLER_STATE_DIR", str(PROJECT / "data" / "crawler")))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A crawler or index worker is already running") from None
        yield


def elasticsearch_client():
    options = {"request_timeout": 20, "max_retries": 0}
    if os.getenv("ELASTICSEARCH_API_KEY"):
        options["api_key"] = os.environ["ELASTICSEARCH_API_KEY"]
    return Elasticsearch(os.getenv("ELASTICSEARCH_URL", "http://localhost:9200"), **options)


def ensure_index(client, index):
    if not client.indices.exists(index=index):
        client.indices.create(index=index, mappings={"properties": {
            "url": {"type": "keyword"}, "title": {"type": "text"},
            "text": {"type": "text"}, "date": {"type": "date"},
            "scraped_on_date": {"type": "date"},
        }})


def sync_sources(store, fetcher, source_file=None):
    if source_file:
        contents = Path(source_file).read_text(encoding="utf-8")
    else:
        contents = fetcher.fetch(SMALLWEB_URL).body.decode("utf-8-sig")
    tasks = source_jobs(contents)
    if not tasks:
        raise ValueError("Source list contains no valid feed URLs; existing jobs were preserved")
    count = store.enqueue(tasks)
    logger.info("Read %s feeds; added %s new jobs", len(tasks), count)


def run(store, args, *, index_only=False):
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    client, fetcher = elasticsearch_client(), Fetcher(delay=args.delay)
    worker = Worker(store, fetcher, workers=args.workers, backfill_depth=args.backfill_depth)
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else float("inf")
    processed = 0
    index = os.getenv("ELASTICSEARCH_INDEX", "pages")
    try:
        with process_lock():
            while not stop.is_set() and time.monotonic() < deadline:
                count = 0
                if not index_only:
                    remaining = args.max_jobs - processed if args.max_jobs else args.workers
                    if remaining <= 0:
                        break
                    count = worker.run_batch(remaining)
                    processed += count
                # An index outage must not stop saving newly fetched pages.
                try:
                    indexed = flush_index(store, client, index)
                except Exception as error:
                    logger.warning("Index unavailable (%s); pending writes retained", type(error).__name__)
                    indexed = 0
                if not count and not indexed:
                    if not args.watch:
                        break
                    stop.wait(30)
    finally:
        client.close()
        fetcher.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    logger.info("Processed %s crawl jobs", processed)


def parser():
    result = argparse.ArgumentParser(description="Daily feed checks with durable jobs and archive backfills")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Create missing database tables; preserves existing pages")
    commands.add_parser("init-index", help="Create the search index if missing")
    sync = commands.add_parser("sync", help="Import the current Small Web feed list")
    sync.add_argument("--source-file", help="Read a local feed list instead (useful for a small pilot)")
    backfill = commands.add_parser("backfill", help="Schedule an archive and sitemap crawl for one blog")
    backfill.add_argument("url", help="Blog homepage URL")
    backfill.add_argument("--budget", type=int, default=10_000, help="Maximum historical jobs for this blog; increase to continue a capped backfill")
    commands.add_parser("status", help="Show queue progress and indexing backlog")
    commands.add_parser("retry-failed", help="Retry failed jobs; never erase pending work")
    commands.add_parser("retry-skipped", help="Reevaluate pages previously skipped by extraction")
    commands.add_parser("reindex", help="Queue all saved pages for indexing; never clear the live index")
    for name, help_text in (("run", "Process pending jobs"), ("index", "Process pending search-index writes")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--watch", action="store_true", help="Wait for newly due work until stopped")
        command.add_argument("--workers", type=int, default=8)
        command.add_argument("--delay", type=float, default=2.0, help="Minimum seconds between requests to a host")
        command.add_argument("--backfill-depth", type=int, default=5)
        command.add_argument("--max-jobs", type=int, default=0, help="Stop after this many crawl jobs (0 = unlimited)")
        command.add_argument("--minutes", type=float, default=0, help="Stop taking new work after this many minutes")
    return result


def main(argv=None):
    load_dotenv(PROJECT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("elastic_transport").setLevel(logging.ERROR)
    args = parser().parse_args(argv)
    if args.command == "backfill" and args.budget < 1:
        parser().error("Backfill budget must be positive")
    if hasattr(args, "workers") and (not 1 <= args.workers <= 32 or not math.isfinite(args.delay) or args.delay < 0 or args.max_jobs < 0 or not math.isfinite(args.minutes) or args.minutes < 0 or not 1 <= args.backfill_depth <= 12):
        parser().error("Use 1–32 workers, depth 1–12, and nonnegative delay, job, and time limits")
    try:
        with Store() as store:
            if args.command == "migrate":
                store.migrate()
            elif args.command == "init-index":
                with elasticsearch_client() as client:
                    ensure_index(client, os.getenv("ELASTICSEARCH_INDEX", "pages"))
            elif args.command == "sync":
                fetcher = Fetcher()
                try:
                    sync_sources(store, fetcher, args.source_file)
                finally:
                    fetcher.close()
            elif args.command == "backfill":
                url = normalize_url(args.url)
                if not url:
                    raise ValueError("Provide a valid HTTP(S) blog URL")
                store.set_backfill_budget(url, args.budget)
                store.enqueue([job("archive", url, priority=20, root=url),
                               job("sitemap", urljoin(url, "/sitemap.xml"), priority=30, root=url)])
            elif args.command == "status":
                print(json.dumps(store.status(), indent=2, default=str))
            elif args.command == "retry-failed":
                store.retry_failed()
            elif args.command == "retry-skipped":
                store.retry_skipped()
            elif args.command == "reindex":
                store.queue_reindex()
            else:
                run(store, args, index_only=args.command == "index")
    except Exception as error:
        logger.error("Command failed (%s). Check configuration and crawler status.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
