# Download and extraction review — 2026-09-07

Scope: actual HTTP requests, robots policy, concurrency, retries, extraction and
saved article records. Scheduling stays off. No production queue run, source
import, saved-post rewrite, production index change or web-service restart.

## Controller and concurrency

`python -m scraper.scraper run` is one Python process on this server, protected by
a file lock. It keeps at most eight worker threads busy by default. Each handles
one job: download, parse/extract, and commit. Free slots refill immediately.

PostgreSQL in Neon holds URL jobs, attempts, due times and 15-minute leases.
Claims admit one live job per hostname. The shared fetcher also serializes requests
to actual destination hosts, including redirects. At most four short database
operations use the pool at once; downloading/extraction do not hold a transaction.

## Reproduced problems and fixes

- **Robots matching:** Protego 0.6.2 replaces Python's basic parser. Tests cover
  wildcards, end anchors, longest matching rules, allow ties, repeated agent groups
  and fractional crawl delays.
- **Extraction:** compared Trafilatura 2.0.0 and 2.2.0 on the same article/comment/
  code fixture. The old settings duplicated paragraphs and code, flattened code
  indentation and included comment spam. Version 2.2.0 with `favor_precision=True`,
  plus explicit navigation/comment removal before fallback extraction, fixed these
  particular failures. The upgrade and required dependencies are locked.
- **Non-articles:** navigation-only pages no longer become posts just because
  their links have text. Recognized HTTP-200 error pages and noindex directives
  are skipped. Recognized browser challenges retry within the normal attempt limit.
- **Text handling:** plain text bypasses HTML extraction. UTF-8 and declared legacy
  encodings work; PostgreSQL-incompatible controls are removed. Tests retain short
  posts, Japanese/French text, tables, repeated lines and code spaces.
- **Rate limits:** Retry-After is no longer shortened to a day, even on exhausted
  feeds. Redirect errors cool the responding host. Memory cooldowns protect other
  threads; active PostgreSQL cooldowns reload at startup. Malicious numeric headers
  are bounded at 100 years to prevent overflow, not at a daily retry interval.
- **Broken robots:** retryable lookup failures cool the host for at least five
  minutes instead of repeating the failed lookup for every queued post. Waiting
  on a cooldown does not itself slide its deadline forward.
- **Restarts:** a local SQLite HTTP cache preserves robots rules and request times.
  A reproduced 120-second robots delay previously caused a fetch-robots/defer-post
  loop across fresh processes; the restarted worker now finishes the post.
- **Dead URLs:** normal rediscovery already preserved failed posts; increasing a
  backfill budget now preserves them too. Known 404/410 feeds recheck monthly, not
  daily, allowing eventual recovery. Explicit `retry-failed` still means retry.

## Download, storage and cleanup

The fetcher GETs robots.txt when needed, then the post. It does not run a browser
or fetch images, scripts, stylesheets or comment widgets. Redirects, public-target
checks, robots rules, per-host spacing, statuses, compression and byte limits apply
before extraction. Validators allow a 304 without downloading unchanged content.

Trafilatura works locally on a parsed HTML tree; it is not asked to download a URL.
No Kagi extraction API is called. Concurrent extraction tests prohibit DNS calls
and verify repeatable results. Raw article HTML is discarded after processing.

PostgreSQL `pages` stores title, URL, text, inferred publication date, fingerprint
and scrape time. A transaction saves changed content and queues its Elasticsearch
update together. Successful index delivery removes the pending index job; failures
stay queued. URL jobs and failed/skipped records remain deliberately: deleting
those would let rediscovery download known dead/rejected pages again.

The unchanged fingerprint policy deduplicates whitespace-normalized text globally,
including aliases. It can also merge separate posts with identical text. This pass
does not redesign identity or rewrite old fingerprints. Dates remain best-effort.

The next worker run creates `data/crawler/http-cache.sqlite3`, under the same
`CRAWLER_STATE_DIR` as the process lock. It holds only public robots policy and
request times, not posts or jobs; it adds no external service or database migration.
Policies usually expire after one hour, extended for long delays within 24 hours.
Startup removes expired policies and request history older than a day; SQLite
reuses freed space. Expired policies are not used by a long-running process either.
Cache errors stop/defer work instead of bypassing rules. To recover corruption,
stop the worker and move that exact cache file aside; PostgreSQL data is unaffected.
Do not routinely remove a healthy cache, which would lose recent request timing.

## Verification and limits

- **218 automated tests**, using disposable PostgreSQL 17 schemas and a uniquely
  named temporary Elasticsearch index; includes concurrency, leases, rollback,
  restart/retry behaviour, index outages and new extraction fixtures. Final pass:
  218/218 passed in 8.830 seconds.
- Dependency lock and installed-package compatibility checks passed.
- Final read-only production check: **314,974** PostgreSQL pages, **314,974**
  Elasticsearch documents, **931** crawl jobs and **0** pending index updates.
  Both crawler/source timers remain uninstalled/inactive; web service is active.
- Six existing public URLs fetched with robots and default throttling, without
  saving results. Five produced content; one archive explicitly said noindex:

| URL | Outcome |
| --- | --- |
| jvns.ca/blog/2023/10/06/new-talk--making-hard-things-easy/ | Article, 32,238 text characters |
| simonwillison.net/2025/Nov/4/datasette-10a20/ | Article, 15,324 characters |
| daverupert.com/2024/03/vibe-check-31/ | Article, 7,037 characters |
| ma.tt/2002/09/greyscale/ | Article, 1,267 characters |
| www.baldurbjarnason.com/notes/bookmarks/ | Link-note page, 10,953 characters |
| www.tbray.org/ongoing/When/202x/2025/11/ | Excluded by noindex |

These smoke checks and targeted fixtures are **not** a statistical quality estimate
across all blogs. Comment removal, soft-404 detection and layout classification
remain imperfect. Previously indexed unwanted pages, including the sampled archive,
have not been retroactively removed. The existing index was not rewritten.

Existing limits remain: no JavaScript rendering; five redirects; 5 MB compressed/
decompressed bodies; 60-second request-chain budget. OS DNS and trickled headers
lack a strict total timeout; DNS rebinding needs network-level egress protection.
Robots spacing of a full day or more is explicitly unsupported and stops the post
job rather than repeatedly resetting a long wait.

References: [Protego](https://github.com/scrapy/protego),
[Trafilatura options](https://trafilatura.readthedocs.io/en/latest/corefunctions.html),
[robots protocol](https://www.rfc-editor.org/rfc/rfc9309.html).
