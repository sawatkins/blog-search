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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urljoin

# Support both python -m scraper.scraper and the old script entry point.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from elasticsearch import Elasticsearch

from scraper.content import SMALLCOMIC_URL, SMALLWEB_URL, job, source_jobs
from scraper.fetching import Fetcher, normalize_url
from scraper.storage import SourceSyncError, Store
from scraper.worker import CrawlPaused, Worker, flush_index

logger = logging.getLogger(__name__)
PROJECT = Path(__file__).resolve().parents[1]


def state_directory():
    return Path(os.getenv('CRAWLER_STATE_DIR', str(PROJECT / 'data' / 'crawler')))


@contextmanager
def process_lock():
    """This deployment has one host; a file lock prevents overlapping workers."""
    directory = state_directory()
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
        # Keep deletion versions well beyond a request timeout/retry window.
        # This is bounded protection, not indefinite retention across restores.
        client.indices.create(index=index, settings={'number_of_shards': 1, 'number_of_replicas': 0,
                                                    'gc_deletes': '1h'}, mappings={"properties": {
            "url": {"type": "keyword"}, "title": {"type": "text"},
            "text": {"type": "text"}, "date": {"type": "date"},
            "scraped_on_date": {"type": "date"},
        }})


def sync_sources(store, fetcher, source_file=None, exclude_file=None, *, allow_large_removal=False):
    if source_file:
        contents = Path(source_file).read_text(encoding="utf-8-sig")
    else:
        contents = fetcher.fetch(SMALLWEB_URL).body.decode("utf-8-sig")
    tasks = source_jobs(contents)
    if not tasks:
        raise SourceSyncError("Source list contains no valid feed URLs; existing jobs were preserved")
    # Kagi's blog and comic lists overlap. Check both before changing the queue.
    # A local exclusion file makes bounded/offline pilots reproducible.
    exclusions = (Path(exclude_file).read_text(encoding="utf-8-sig") if exclude_file else
                  fetcher.fetch(SMALLCOMIC_URL).body.decode("utf-8-sig"))
    excluded = [task["url"] for task in source_jobs(exclusions)]
    if not excluded:
        raise SourceSyncError("Comic exclusion list is empty or invalid; existing jobs were preserved")
    tasks = source_jobs(contents, excluded)
    if not tasks:
        raise SourceSyncError("No blog feeds remain after comic exclusions; existing jobs were preserved")
    result = store.reconcile_sources(tasks, excluded, replace=not bool(source_file),
                                     allow_large_removal=allow_large_removal)
    logger.info("Source sync: %s", result)


def run(store, args, *, index_only=False):
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else float("inf")
    processed = 0
    index = os.getenv("ELASTICSEARCH_INDEX", "pages")
    last_flush = float("-inf")

    def index_pending(*, force=False):
        nonlocal last_flush
        if not force and time.monotonic() - last_flush < 2:
            return 0
        last_flush = time.monotonic()
        # An index outage must not stop saving newly fetched pages.
        try:
            return flush_index(store, client, index)
        except Exception as error:
            logger.warning("Index unavailable (%s); pending writes retained", type(error).__name__)
            return 0

    try:
        with ExitStack() as resources:
            resources.enter_context(process_lock())
            client = elasticsearch_client()
            resources.callback(client.close)
            fetcher = Fetcher(delay=args.delay, cooldowns=store.host_cooldowns(),
                              cache_path=state_directory() / 'http-cache.sqlite3')
            resources.callback(fetcher.close)
            worker = Worker(store, fetcher, workers=args.workers, backfill_depth=args.backfill_depth)
            while not stop.is_set() and time.monotonic() < deadline:
                count = 0
                if not index_only:
                    remaining = args.max_jobs - processed if args.max_jobs else None
                    if remaining is not None and remaining <= 0:
                        break
                    count = worker.run(max_jobs=remaining, stop=stop, deadline=deadline, tick=index_pending)
                    processed += count
                indexed = index_pending(force=True)
                if not count and not indexed:
                    if not args.watch:
                        break
                    stop.wait(30)
    finally:
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
    sync.add_argument("--exclude-file", help="Use a local comic feed list instead of downloading Kagi's")
    sync.add_argument("--allow-large-removal", action="store_true", help="Allow a reviewed upstream snapshot to deactivate over 20%% of active sources")
    backfill = commands.add_parser("backfill", help="Schedule an archive and sitemap crawl for one blog")
    backfill.add_argument("url", help="Blog homepage URL")
    backfill.add_argument("--budget", type=int, default=10_000, help="Maximum historical jobs for this blog; increase to continue a capped backfill")
    status = commands.add_parser("status", help="Show queue progress and indexing backlog")
    status.add_argument("--site", help="Also show historical progress for this exact blog root URL")
    commands.add_parser("retry-failed", help="Retry failed jobs; never erase pending work")
    retry = commands.add_parser("retry-skipped", help="Retry pages with no extractable text; preserve policy skips")
    retry.add_argument('--limit', type=int, default=100, help='Maximum pages to requeue (default: 100); does not fetch them')
    commands.add_parser("reindex", help="Queue all saved pages for indexing; never clear the live index")
    for name, help_text in (("run", "Process pending jobs"), ("index", "Process pending search-index writes")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--watch", action="store_true", help="Wait for newly due work until stopped")
        command.add_argument("--workers", type=int, default=8)
        command.add_argument("--delay", type=float, default=5.0, help="Minimum seconds between requests to a host")
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
    if args.command == 'retry-skipped' and args.limit < 1:
        parser().error('Retry limit must be positive')
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
                    sync_sources(store, fetcher, args.source_file, args.exclude_file,
                                 allow_large_removal=args.allow_large_removal)
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
                root = normalize_url(args.site) if args.site else None
                if args.site and not root:
                    raise ValueError("Provide a valid HTTP(S) blog URL")
                print(json.dumps(store.status(root=root), indent=2, default=str))
            elif args.command == "retry-failed":
                store.retry_failed()
            elif args.command == "retry-skipped":
                logger.info('Requeued %s extraction skips', store.retry_skipped(limit=args.limit))
            elif args.command == "reindex":
                store.queue_reindex()
            else:
                run(store, args, index_only=args.command == "index")
    except Exception as error:
        if isinstance(error, (SourceSyncError, CrawlPaused)):
            logger.error("%s", error)
        else:
            logger.error("Command failed (%s). Check configuration and crawler status.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
