# Crawler cleanup, before automation

Scope: improve discovery, filtering, worker scheduling, and backfill visibility.
No scheduled crawling, whole-list import, web restart, or live index changes.

## Changes

- Parse declared RSS/Atom pagination (`next`, `prev-archive`) as `feed_page` jobs.
  These are historical work, not new daily feeds. They share the blog's lifetime
  budget and have a separate 20-hop pagination limit. Entries from them are lower
  priority than current feed entries. Existing blogs participate too.
- Follow HTML head pagination links, deduplicating matching body links. On an
  individual article, a next-article link remains an article candidate, not a
  listing that would be excluded from indexing.
- Reject obvious utility URLs during feed, sitemap, and HTML discovery and before
  indexing queued pages. Preserve short posts and dated article slugs.
- Import the blog feed list minus exact normalized feeds in Kagi's comic list.
  Fetch/validate exclusions before writes; failed exclusions leave the queue alone.
  Optional local source/exclusion files allow reproducible offline pilots.
- Replace the CLI's batch barrier with a bounded, continuously refilled thread
  pool. PostgreSQL still controls per-host leases, retries, and durable completion.
  Index writes are flushed periodically, with slots refilled before flushing.
- Accept shallower historical discovery paths without consuming budget again.
  Revisit depth-limited successes/skips with full responses, preserve failed jobs
  and retry delays, and never revoke a running lease.
- Add `status --site URL`, reporting budget, historical jobs visited, due work,
  failures and skips. Explicitly distinguish capped history from completeness.

## Verification

- **175 tests passed**, including real PostgreSQL 17 transactions and migrations,
  claim exclusivity, retries, interruption, indexing outages, and new regression
  tests. The disposable test database was separate from Neon.
- New end-to-end fixture: a post appears only on feed page two, not on the current
  feed, homepage, or sitemap. It reaches stored content and the index outbox. The
  contact page advertised alongside it is never fetched or stored.
- A synchronization test requires the third job to start while the first slow job
  is still running. This detects the old batch barrier without timing thresholds.
- Synthetic comparison, two workers and 12 mocked waits: batches **0.727s**,
  continuously refilled workers **0.426s**. This is an orchestration comparison,
  not a claim about real-world crawl speed or database throughput.
- Public-list check, without importing anything: **40,627** valid blog feeds,
  **414** comic feeds, **23** overlapping feeds excluded, **40,604** eligible feeds.
  Sources: [Kagi's repository](https://github.com/kagisearch/smallweb#info),
  [blog list](https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt),
  [comic list](https://raw.githubusercontent.com/kagisearch/smallweb/main/smallcomic.txt).
- Applied the additive job-kind constraint/index migration to Neon under the
  crawler process lock. Before/after crawl-row digests match; **931** jobs,
  **314,974** saved pages and **0** pending index writes are unchanged.
- Both crawler/source timers remain `not-found` and `inactive`. The six pilot
  feeds are still the only imported crawler sources. No worker was started.

## Remaining limits and decisions

This is bounded historical discovery, not proof of a complete blog archive. There
is still no JavaScript rendering, no recovery of completely unlinked posts, and
no reliable universal classifier for mixed comic/text blogs or unusual utility
pages. Filters do not retroactively purge the existing index. Source sync adds
eligible feeds; it does not yet deactivate previously imported sources that have
been removed or newly classified as comics upstream. Address that before a full
automated source import.

All six pilot histories remain budget-limited. Increasing a budget is an explicit
backfill action, not a daily allowance reset. A missing feed homepage does not
justify guessing that an entire shared hostname belongs to that blog.

Keep durable crawl jobs, saved content and the pending-index outbox in PostgreSQL:
completion is atomic and restarts do not lose work. Keep detailed logs on this
server. Neon versus local PostgreSQL is a separate hosting/cost decision; this
cleanup does not establish that Neon is cheapest, and does not justify adding a
second queue system. Scheduling remains a separate, unapproved step.
