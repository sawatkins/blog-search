# Run safety and controlled extraction retries — 2026-09-07

Scope: stop a crawler-wide failure from consuming an unlimited stream of jobs,
and make manual retries after extractor changes bounded and narrowly targeted.
No new service, schema migration, source enrollment, schedule, production crawl,
existing-index cleanup or identical-text deduplication change.

## Whole-run stop

The previous worker recorded each failure and continued claiming other jobs.
The new guard watches connection/DNS failures before HTTP response headers.
Five such failures within five minutes, involving at least three actual failing
hostnames, stop intake unless a successful fetch or HTTP error response intervenes.
Redirect failures count against the destination, not the feed-link hostname.
The short history is bounded and protected across worker threads.

Normal HTTP responses (including 404, 429 and 503) demonstrate connectivity and
reset this history. Robots policy denial, certificate problems, local host waits,
body decoding errors and malformed feeds/sitemaps do not count as connection
failures. Invalid or truncated gzipped sitemaps remain individual job failures.
Unexpected internal exceptions instead stop immediately: rerunning a programming
bug eight times is not evidence that the article is permanently unavailable.

After a stop:

- No more work is intentionally claimed. A claim already in progress may return;
  those tasks are released without fetching. At most the configured worker count
  can already be in flight; successful downloads still save normally.
- Affected owned jobs return to `pending` with a five-minute delay, a diagnostic
  reason, and their current attempt refunded. Validators, saved pages and pending
  index writes are preserved. Stale/expired acknowledgements cannot release a
  newer worker's lease; source removal still wins.
- Earlier failures already recorded before the pattern appeared keep their
  ordinary retries, including terminal exhaustion if they were on attempt eight.
  The guard does not retroactively declare those URLs healthy. It limits the
  damage of an outage; it does not promise that outages consume zero retries.
- If PostgreSQL itself cannot accept the deferral, an error is logged and the
  existing 15-minute lease-expiry path remains the fallback. That path retains
  the ordinary maximum-attempt rules, rather than silently promising a refund.
- The CLI drains in-flight work and exits with status 1 and an actionable reason,
  including under `--watch`. Another invocation of the same Worker object stays
  stopped. An operator must investigate and start a new run; this is not an
  automatic restart loop. Saved outbox entries remain available to `index`.

This is intentionally a conservative guard, not a connectivity diagnosis. Several
unrelated dead domains can trigger it. Intermittent outages with successful replies,
or a shared failing destination, may not trigger it. The latter stays subject to
normal per-host cooldowns and per-URL retries. No external health-check dependency
or durable run-state table was added.

## Extraction retry

`retry-skipped --limit 100` now requeues only active page jobs whose saved reason is
`no_extractable_text`, oldest first. It defaults to 100 and rejects nonpositive
limits before opening the database. It clears ETag/Last-Modified so the next worker
run obtains a body, not a 304 with nothing to re-extract. The command only queues
work; it does not start downloads or scheduling.

Noindex, utility-page, unsupported-content-type, archive-listing and removed-source
skips are left alone. This replaces the old command's unbounded retry of every
skipped page; no extractor-version table or automatic deployment-triggered crawl
was necessary. Successful feed/article recheck intervals are unchanged.

## Verification

Full suite: **276 tests passed in 12.541 seconds**, including PostgreSQL and
Elasticsearch integration tests. `git diff --check` and retry-command help passed.

- The isolated PostgreSQL outage test queues 100 URLs: a serial worker attempts
  five, stops with 95 untouched, records four ordinary retries and refunds the
  triggering job. All 100 jobs remain pending with no live leases. A healthy new
  worker subsequently completes all 100 after the test advances their due times.
- Parallel tests bound intake, account for every claim, and demonstrate that an
  already-running healthy download can finish after the guard trips.
- Tests cover one shared redirect destination, success/HTTP-error streak resets,
  time-window expiry, normal site failures, corrupt sitemaps, internal exceptions,
  failed database release, stale leases and source removal.
- CLI tests cover a nonzero explanatory exit, `--watch` not restarting, resource
  closure, restored signal handlers, and bounded queue-only extraction retry.
- Real PostgreSQL tests verify deferral on attempt eight refunds to seven,
  preserves pages/validators/outbox entries, and does not affect newer leases.
- Full suite uses disposable localhost PostgreSQL and a UUID-named Elasticsearch
  test index. Neither Neon nor the live `pages` index is written by these tests.
