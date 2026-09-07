# Running the crawler on this server

The app stays in `blogsearch.service`, Elasticsearch stays in Docker, and
PostgreSQL stays on the configured database (currently Neon). New crawl jobs and
pending index writes live in PostgreSQL. SQS is no longer part of the crawler;
`scraper/sqs_queue.py` remains available to inspect the old queue during migration.
No command in the new crawler reads or purges that queue.

## Configuration

Run commands from `/home/debian/blog-search`. Existing `.env` settings work:
`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`,
`PGCHANNELBINDING`, and `ELASTICSEARCH_URL`. The crawler also accepts `DATABASE_URL`.
An optional `ELASTICSEARCH_API_KEY` authenticates search and indexing;
`ELASTICSEARCH_INDEX` defaults to `pages`. Do not put secrets
in service files or source control.

Use `.venv/bin/python` on this production machine to avoid updating the live
environment inadvertently. For a fresh checkout, install with `uv sync --frozen`.

## Prepare and pilot

1. Verify your database backup/restore arrangements before production migration.
2. Run `.venv/bin/python -m scraper.scraper migrate`. It creates crawl tables and
   preserves existing pages. It does not start crawling or alter Elasticsearch.
3. Run `.venv/bin/python -m scraper.scraper init-index` if provisioning a new search
   index. The existing `pages` index is left in place.
4. Create a small feed list and run
   `.venv/bin/python -m scraper.scraper sync --source-file /path/to/pilot-feeds.txt`.
5. Run `.venv/bin/python -m scraper.scraper run --max-jobs 20 --workers 2`.
6. Inspect `.venv/bin/python -m scraper.scraper status` and the resulting pages.

Do not run the retired batch scraper simultaneously with the replacement. Inspect
any old SQS backlog before retiring AWS resources; rediscovery cannot guarantee
recovery of URLs that have already fallen out of feeds. Existing `skipped_urls`
are not imported: the old URL stripping and short-post rules caused false skips.

## Daily operation

After the pilot, `sync` imports the whole Small Web list. A successful feed check
is due again after one day. Successful pages, sitemaps, and archive pages are
revisited after 30 days. New blogs automatically start archive/sitemap discovery.
Discovery never resets existing work, and fresh feed entries have priority over
historical crawling.

The worker timer wakes every 15 minutes to process due jobs and retries; it does
not check every blog every 15 minutes. It exits when caught up instead of holding
Neon's database awake with constant polling. Eight workers and a two-second
minimum interval per host are conservative defaults. HTTP validators avoid
downloading unchanged feeds/pages.

Install the four provided units in `/etc/systemd/system/`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now blogsearch-sources.timer blogsearch-crawler.timer
```

View logs with `journalctl -u blogsearch-crawler.service -u blogsearch-sources.service`.
After verifying the backend changes, restart `blogsearch.service` to load them.
The application no longer creates database tables at startup; run migrations first.

## Backfill and recovery

- `backfill https://example.org/blog/` schedules that blog's archive and sitemap.
  Following links is limited to the blog's hostname/path and five archive levels.
  Sitemap indexes have a separate eight-level limit. This is a bounded crawl, not
  a promise to discover every historical page. Pages requiring JavaScript are not
  rendered. XML and gzip sitemaps are supported within response size limits.
  Each blog has a persisted budget of 10,000 historical jobs, including discovery
  pages. Fresh feed entries do not consume this budget. To continue a capped
  backfill, rerun `backfill` with the same root and a larger `--budget`; previously
  discovered pages are not charged again. `status` reports capped backfills.
  Feeds with no homepage still yield posts, but do not trigger a whole-host crawl.
- `retry-failed` reactivates exhausted/permanent failures for another attempt.
- `retry-skipped` reevaluates pages skipped by extraction, for example after
  changing extraction settings.
- `reindex` queues all saved pages for Elasticsearch without clearing the live
  index. Run `index` to drain those writes, or let the regular worker do it.
  This repairs missing/changed documents; it does not remove stale ES-only IDs.
- `status` reports pending, failed, skipped, and leased jobs plus indexing backlog.
- Stop the crawler timer and service to pause. Completed progress remains saved;
  leases from an interrupted worker expire and can be recovered automatically.

Rate-limit responses pause other jobs for the same host until the cooldown expires.
Search writes are recorded in the same transaction as content. Elasticsearch
outages therefore leave a recoverable backlog. Per-page bulk failures are retried;
an old acknowledgement cannot erase a newer version waiting to be indexed.

## Tests

```sh
.venv/bin/python -B -m unittest discover -s tests
```

Database tests are skipped unless `TEST_DATABASE_URL` points to an **isolated test
database**. They create temporary schemas and test real transactions, leases,
deduplication, and index recovery. Never point this setting at production.

The queue query was benchmarked locally with 300,000 synthetic pending jobs across
10,000 hosts. Selecting eight jobs took about 780 ms with a full per-host sort and
3 ms with the indexed selection used here (median of seven runs). These numbers
measure queue selection only, not network fetching or Neon latency. Index success
and failure acknowledgements are also batched into one transaction per bulk response.

The unit files are templates for this machine. Merely checking them into the repo
does not install timers, migrate Neon, restart the web app, or start a mass crawl.
