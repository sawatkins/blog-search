# Robots/TLS retry follow-up — 2026-09-07

This supersedes the robots/TLS retry behavior described in earlier pilot reports.
Scheduling remains off. No source-list import, article/index cleanup or HTTP
fallback was enabled; no new queue, database tables or infrastructure services.

## Policy

| Outcome | Next check |
| --- | --- |
| Successful feed | One day, unchanged |
| Robots.txt denies a URL | 30 days from the first denial, feeds and posts |
| TLS/certificate connection failure | Seven days from the first failure, feeds and posts |
| Missing feed, HTTP 404/410 | 30 days, unchanged |
| Missing post, HTTP 404/410 | Retained terminal failure, unchanged |
| Other temporary failures | Existing bounded exponential retries, up to eight attempts per URL; exhausted feeds recheck daily |

Eight attempts meant executions of the same queued URL over time, not eight posts
and not eight HTTP attempts in one request. Robots and TLS problems now bypass
that short-retry cycle. `FetchError.failure_kind` communicates the category to
`Store.fail`; human-readable errors and next due times remain in `crawl_jobs`.
Scheduled policy rechecks reset the short-retry counter, so they cannot become
permanent failures just by repeatedly observing unchanged policy/configuration.
Successful checks return to the ordinary feed/page schedule.

A URL's monthly/weekly due time is not a host-wide cooldown. A denied feed path
does not block allowed article paths. Existing brief cooldowns for broken robots
lookups are preserved, but a TLS error must not impose a seven-day host ban that
also blocks working HTTP. Server-requested longer Retry-After values are never
shortened. Normal robots-cache expiry remains unchanged (usually one hour, at
most a day for long-delay policies): defer crawling, don't rely on month-old
robots permissions to crawl other URLs.

The idempotent compatibility update in `db/crawler.sql` targets only known legacy
`Blocked by robots policy` errors with failed or too-early pending states. It
preserves unrelated failures, longer due times and active leases. Generic legacy
network errors are not guessed to be certificate failures; future requests
classify them accurately. The existing live robots-denied post is covered by this
update. The three failed feeds from the broad pilot remain local report entries,
not enrolled production sources.

## yeikoff.xyz: HTTP works; HTTPS is misconfigured

Read-only checks through the normal robots-aware, five-second-spaced fetcher:

- `http://yeikoff.xyz/`: HTTP 200, HTML homepage.
- `http://yeikoff.xyz/blog/index.xml`: HTTP 200, parsed 11 post links.
- The HTTP feed declares `https://yeikoff.xyz/blog/` as its homepage and advertises
  **HTTPS** post URLs. Changing just the feed scheme therefore would not fix
  fetching the advertised posts.
- HTTPS fails on `/robots.txt` with a certificate hostname mismatch. The final
  HTTPS-feed probe then observes the normal short host cooldown; it is not a
  separate certificate measurement.

This is not evidence that the blog is gone. It distinguishes a working HTTP
endpoint from a broken HTTPS endpoint. Certificate validation remains enabled;
the crawler does not automatically downgrade supplied HTTPS links to HTTP or
silently accept a bad certificate. Explicit HTTP URLs remain supported, subject
to the normal network/robots rules.

## Verification and production scope

Full test suite, including real PostgreSQL schemas and local HTTP/TLS tests:
**251 tests passed**. New coverage includes first-failure intervals, restart,
rediscovery, stale leases, recovery after recheck, robots paths that allow other
pages, TLS error classification, no TLS bypass/downgrade, preservation of longer
server delays, short host cooldowns, and idempotent legacy-policy migration.

Production changes are limited to the new code and rescheduling the one known
robots-denied queue row. No article or search document is changed, no crawler
worker is started, and the timers remain inactive. The disposable test database
and uniquely named test index are removed after verification.

The rescheduled job is ID 706, now due 2026-10-07 06:42:59 UTC. Before/after
records are retained in ignored `backup/retry-policy-20260907/`. PostgreSQL and
Elasticsearch both retain 314,974 articles/documents; the queue still has 931 jobs
and no pending index writes. Source enrollment remains the same six pilot feeds.

References: [Robots cache rules, RFC 9309 §2.4](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.4),
[Requests TLS verification](https://requests.readthedocs.io/en/stable/user/advanced/#ssl-cert-verification).
