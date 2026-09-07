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
Python is pinned to 3.14.7; use uv 0.12.10 or newer. The supported search server is
Elasticsearch 9.3+ within major 9. See the [runtime/update audit](audits/2026-09-07.md)
for deployed versions, security fixes, verified snapshots and the pending reboot.

## Prepare and pilot

1. Verify your database backup/restore arrangements before production migration.
2. Run `.venv/bin/python -m scraper.scraper migrate`. It creates crawl tables and
   adds the missing cooldown column to older `domains` tables and permits the
   `feed_page` job kind and source-ownership tables, preserving feed
   history and existing pages. It also adds `crawl_jobs.resolved_url`, associating
   validators with the final response URL. The index-cleanup migration also replaces
   global text uniqueness with a nonunique fingerprint lookup, adds `page_aliases`,
   and allows durable index deletions. It does not start crawling or alter Elasticsearch.
   Schema-lock waits are limited to five seconds; retry the migration if the
   database is busy rather than leaving a lock request queued behind live traffic.
3. Run `.venv/bin/python -m scraper.scraper init-index` if provisioning a new search
   index. The existing `pages` index is left in place.
4. Create a small feed list and run
   `.venv/bin/python -m scraper.scraper sync --source-file /path/to/pilot-feeds.txt`.
   This still downloads Kagi's comic exclusion list. For an offline/reproducible
   pilot, also supply `--exclude-file /path/to/smallcomic.txt`.
5. Run `.venv/bin/python -m scraper.scraper run --max-jobs 20 --workers 2`.
6. Inspect `.venv/bin/python -m scraper.scraper status` and the resulting pages.

Do not run the retired batch scraper simultaneously with the replacement. Inspect
any old SQS backlog before retiring AWS resources; rediscovery cannot guarantee
recovery of URLs that have already fallen out of feeds. Legacy `skipped_urls`
were not imported: the old URL stripping and short-post rules caused false skips.
The backed-up index cleanup retires that table and the unused legacy `feeds`
table; current feed membership and retry history remain in the crawl tables.

## Daily operation (not enabled)

Download/extraction details are recorded in
[the focused review](pilots/2026-09-07-download-review.md), followed by the
[48-feed/network pass](pilots/2026-09-07-broad-network.md). The next worker run
creates a disposable local HTTP-policy cache in `CRAWLER_STATE_DIR`; URL jobs,
results and rate-limit retry state remain in PostgreSQL. Known 404/410 feeds are
rechecked monthly; genuinely terminal failed post URLs require an explicit retry.
Robots-denied URLs now recheck after 30 days, and TLS/certificate failures after
seven days, from the first failure, for feeds and posts alike. These policy checks
do not consume the short-retry attempt budget. Other temporary failures retain
bounded exponential retries (up to eight attempts per URL, not eight posts or eight
immediate requests); exhausted feeds fall back to daily rechecks. Successful feeds
still check daily. See the [retry-policy follow-up](pilots/2026-09-07-retry-policy.md).
The migration also moves legacy robots-denied jobs onto the monthly policy without
reactivating 404s or changing active leases. Repeating it does not postpone jobs again.

The worker now stops intake on five connection/DNS failures involving at least
three actual destination hosts within five minutes, without an intervening
successful fetch or HTTP error response. This is a conservative suspected-outage
guard, not proof that the machine is offline. Ordinary 404/429/503 responses,
robots denials, TLS errors and malformed documents do not count toward it.
Unexpected internal errors also stop the run. In-flight successes still save;
unstarted claims and outage failures after the stop are returned to pending,
with their attempt refunded and a five-minute delay. Earlier recorded URL failures
retain their ordinary retry policy. If the database cannot record a deferral,
the existing lease-expiry recovery applies. The reason is logged, the CLI exits
nonzero even under `--watch`, and a new run must be started after investigation.
See the [run-safety tests and limits](pilots/2026-09-07-run-safety.md).

Automation is deliberately off while crawler behavior is being reviewed. The
following describes the intended operation, not an installed schedule.

`sync` imports `smallweb.txt` minus the normalized feed URLs in `smallcomic.txt`.
The lists overlap; merely importing the blog list does not exclude all known
comic feeds. Both lists must be readable and valid before enqueueing anything.
This is not a classifier for comics within mixed-content blogs and does not delete
already indexed content. Full upstream sync now deactivates removed/comic sources
and pauses jobs with no active owner. Shared URLs remain eligible if another active
source owns them. Removal during a fetch discards its results without releasing
the host early. Reappearing sources are rechecked. A drop exceeding 20% of active
sources aborts atomically unless the operator explicitly uses `--allow-large-removal`
after inspecting the snapshot; unavailable/invalid lists never deactivate sources.
Local `--source-file` imports enroll their selected feeds without removing others.
Explicit manual jobs remain independent of upstream membership. Legacy jobs are
also preserved as independent by default; this server's six verified pilot feeds
and their existing jobs were explicitly migrated to managed source ownership.

A successful feed check is due again after one day. Successful pages, sitemaps,
archive pages, and historical feed pages are revisited after 30 days. Every feed
response with a homepage seeds archive/sitemap discovery, including existing blogs.
Rediscovery preserves progress, but a shorter historical route can reopen an
earlier depth cutoff. Fresh feed entries have priority over historical crawling.

The worker timer wakes every 15 minutes to process due jobs and retries; it does
not check every blog every 15 minutes. It exits when caught up instead of holding
Neon's database awake with constant polling. Eight workers and a five-second
minimum interval per host are conservative defaults. Worker slots refill as jobs
finish, without waiting for the slowest host in a batch. Only available slots are
claimed from PostgreSQL, and at most one job per host is leased at once. Stop/time
limits stop new claims and allow already claimed work to finish. HTTP validators avoid
downloading unchanged feeds/pages.

The four provided units are templates only. Do not install or enable them until
the crawler review and the user's automation decision are complete.

View logs with `journalctl -u blogsearch-crawler.service -u blogsearch-sources.service`.
The web service was restarted with the tested runtime on 2026-09-07. For future
backend changes, verify first and then restart `blogsearch.service` to load them.
The application no longer creates database tables at startup; run migrations first.

## Backfill and recovery

- `backfill https://example.org/blog/` schedules that blog's archive and sitemap.
  This works for existing blogs too; it is not restricted to newly imported feeds.
  Following links is limited to the blog's hostname/path and five HTML link levels.
  Historical HTML pages also yield links, so unfamiliar archive names and calendar
  pages are not dead ends. Fresh feed posts are extracted without this extra traversal.
  Sitemap indexes have a separate eight-level limit. This is a bounded crawl, not
  a promise to discover every historical page. Pages requiring JavaScript are not
  rendered. XML and gzip sitemaps are supported within response size limits.
  Recognizable home, post-list, category, and tag URLs are traversed as listings,
  including when supplied by a sitemap, rather than indexed as individual posts.
  Declared RSS/Atom next-page links (including `prev-archive`) are followed as
  historical feed pages, with a separate 20-page depth limit and the same SQL
  budget. HTML head pagination links are followed as well as ordinary anchors.
  JSON Feed `next_url` pagination is supported too.
  Obvious utility URLs (contact/privacy/login, etc.) are rejected during discovery
  and checked again before indexing queued pages. Short posts and dated article
  slugs are retained; these conservative rules cannot identify every non-article.
  Each blog has a persisted budget of 10,000 historical jobs, including discovery
  pages. Fresh feed entries do not consume this budget. To continue a capped
  backfill, rerun `backfill` with the same root and a larger `--budget`; previously
  discovered pages are not charged again. This rechecks archive/sitemap/feed-page jobs and
  historical pages, clearing HTTP validators so omitted links can be rediscovered.
  Fresh feed jobs, recent feed posts, other blogs, running leases and terminal
  failures are left alone. Dead URLs are not revived by increasing a budget.
  `status` reports capped backfills; `status --site https://example.org/blog/`
  shows one root's budget, visited jobs, failures, and due work. Use the exact root
  including its scheme/path. A zero due count does **not** prove that all older
  posts were found. The cap is a lifetime job budget, not a daily allowance.
  Feeds with no homepage still yield posts, but do not trigger a whole-host crawl.
- `retry-failed` explicitly reactivates exhausted/permanent failures, including
  dead URLs. Do not use it as part of ordinary daily operation.
- `retry-skipped --limit 100` explicitly requeues up to 100 active pages marked
  `no_extractable_text`, oldest first, after an extraction improvement. It clears
  HTTP validators so the next worker run downloads the body again. It neither
  downloads pages itself nor reopens noindex, archive, utility or non-HTML skips.
  The default limit is 100; larger batches require an explicit positive limit.
  This is not an automatic retry after every code deployment.
- `reindex` queues all saved pages for Elasticsearch without clearing the live
  index. Run `index` to drain those writes, or let the regular worker do it.
  This repairs missing/changed documents; it does not remove stale ES-only IDs.
- `status` reports pending, failed, skipped, and leased jobs plus indexing backlog.
  It also reports active/inactive sources and source-paused jobs. Paused jobs are
  retained for recovery but excluded from the due-work count.
- Stop the crawler timer and service to pause. Completed progress remains saved;
  leases from an interrupted worker expire and can be recovered automatically.

Rate-limit responses pause other jobs for the same host until the cooldown expires.
DNS answers are checked and pinned to the socket connection, including on redirects;
the normal hostname, TLS verification, robots policies and five-second spacing remain.
Do not replace this adapter with a plain Requests call for untrusted crawl URLs.
Original URL jobs remain distinct so redirects can be rechecked, but article storage
uses the final URL. Validators are sent only to their saved final URL, even when
it is reached through a tracking service. HTTP/HTTPS and trailing slashes are not
blindly merged: those URLs can really serve different content without a redirect.
`page_aliases` remembers verified alternative URLs under one stable article ID.
Matching text can also establish HTTP/HTTPS/trailing-slash variants of the same
host/path; matching words on unrelated URLs do not establish identity. Redirects
between two already-saved IDs merge them and enqueue deletion of the extra ES
document. Title cleanup strips leading emoji grapheme clusters at both extraction
and storage boundaries, preserving accents, numbers, other scripts and interior emoji.
A redirected homepage can move historical discovery scope; child jobs keep the
original budget root and carry `payload.scope_root` for the verified new scope.
Ordinary post and CDN sitemap redirects do not authorize a whole-site scope change.
Search writes are recorded in the same transaction as content. Elasticsearch
outages therefore leave a recoverable backlog. Per-page bulk failures are retried;
an old acknowledgement cannot erase a newer version waiting to be indexed.
Elasticsearch also receives the SQL revision as an external version, preventing a
delayed old request from replacing newer content. Unexpected version conflicts
remain visible/retryable, not silently acknowledged. Before adapting this migration
to another existing index, verify SQL revisions exceed its legacy document versions.
That check was completed for this server. After restoring an older database, rebuild
a separate search index or reconcile version counters before reusing the live one.

The outbox holds `index` and `delete` operations. A missing document acknowledges
a delete, but an absent index, version conflict or other error remains retryable.
Do not directly delete `pages` rows with ad-hoc SQL: use the transactional deletion
helper so search does not retain orphaned results. The outbox intentionally has no
page foreign key because a pending deletion must outlive its article row.
`index.gc_deletes=1h` retains deletion versions beyond the ordinary request/retry
window. Elasticsearch's delete-version memory is finite, not an indefinite fence:
do not replay old in-memory batches or restore stale outboxes into the live index.

See the [existing-index cleanup](audits/2026-09-07-index-cleanup.md) for the full
database/index comparison, restored backup rehearsal, removed legacy tables,
retained archive, and the remaining quality limits. The maintenance scripts are
one-off audited-manifest tools, not part of scheduled crawling.

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

The [2026-09-07 production pilot](pilots/2026-09-07.md) records the first live
verification. The subsequent [six-blog backfill pilot](pilots/2026-09-07-backfill.md)
verified older posts and records discovery fixes, final limits, and cleanup.
Scheduled crawling is still off.

The [crawler cleanup review](pilots/2026-09-07-crawler-cleanup.md) records pagination,
filtering, concurrency and backfill-status fixes, with remaining coverage limits.
The [ingestion review and system map](REVIEW.md) is the current record of what has
been checked, source lifecycle behavior, verification, and the review boundary.
