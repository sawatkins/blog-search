# Ingestion review and system map — 2026-09-07

## What the pieces do

1. **Kagi's lists** identify the blog feeds to consider. Full sync reconciles the
   blog list against the comic exclusions and previously registered sources.
2. **The crawler runs on this server.** Feeds supply recent post URLs. Sitemaps,
   archive links and feed pagination supply older URLs, including for existing blogs.
3. **Neon hosts PostgreSQL.** `crawl_jobs` is the durable URL queue, not a separate
   Neon queue product. Workers claim up to eight jobs, one per hostname, download
   them, extract article text with Trafilatura, and save results in `pages`.
4. **A second PostgreSQL table, `crawl_index_jobs`, is the pending-index queue.**
   Saving a changed post and adding its index update happen in the same transaction.
   The index worker sends that content to Elasticsearch and acknowledges successful
   versions. Failed delivery stays queued; crashes do not erase pending work.
5. **Elasticsearch runs locally in Docker** and serves the main search index.
   The FastAPI web backend runs in `blogsearch.service`, serves search requests and
   reads PostgreSQL for supporting data. Neither PostgreSQL nor Elasticsearch is
   the crawler itself.

AWS SQS was the old URL queue. The replacement has no SQS imports or calls. The old
helper remains only for migration inspection. A read-only metadata check found the
old queue still exists, with approximately **0 available and 0 in-flight messages**
and a **345,600-second (four-day) retention setting**. No messages were received,
purged, imported or deleted during this review. The current empty count does not
establish what happened to messages historically.

## Reviewed boundary

Follow-up: the deeper download/extraction pass reproduced gaps not covered by the
earlier tests. See [the download review](pilots/2026-09-07-download-review.md) for
the fixes, 218-test result, live read-only checks and remaining quality limits.
The subsequent [runtime audit](audits/2026-09-07.md) records the five-second default,
Python/dependency/Elastic security upgrades, 219 tests, snapshot restore check and
actual web-service restart. Earlier version/count notes below are historical.
The subsequent [existing-index cleanup](audits/2026-09-07-index-cleanup.md)
supersedes the old content-count/deduplication notes: explicit aliases replace
global text uniqueness, and index deletions are durable outbox operations.

This review covers ingestion from source membership through search-index delivery.
It is not a claim that the entire application or deployment has been reviewed.

| Area | Checked in this review | Evidence / outcome |
| --- | --- | --- |
| Source membership | Removal, comic exclusion, reappearance, partial imports, suspect snapshots | Atomic source reconciliation and source-owned jobs; regression tests |
| Shared ownership | Shared pages, shared archives, already visited/skipped discovery pages | Removing one owner does not disable another; new owners trigger rediscovery |
| In-flight removal | Completion/failure after source removal | No saved content, new jobs, or host cooldown from a now-ineligible result; lease remains until return/expiry |
| Fetching | Public-target checks, redirects, robots, throttling, compression/size limits, HTTP retries, conditional requests | Existing adversarial tests rerun; unsolicited 304 now rejected |
| Parsing/discovery | RSS, Atom, JSON Feed, nested/compressed sitemaps, HTML pagination, root scope, depth and job caps | JSON pagination added; RSS/Atom parsing avoids unused content/media processing |
| Queue/storage | Concurrent claims, expired/stale leases, rollback, retry backoff, deduplication, ownership, connection recovery | Real PostgreSQL integration tests; updated source-aware query benchmark |
| Index delivery | Atomic outbox, partial failures, outage/restart behavior, stale acknowledgements, delayed old writes | External SQL revision ordering; real Elasticsearch retry/out-of-order test |
| Worker lifecycle | Bounded concurrency, slow-host refill, job/time/stop limits, partial initialization | Regression tests; resource cleanup and signal restoration fixed |
| Live migration | Existing pilot ownership and compatibility with legacy ES versions | Backed up, migrated and verified without running a crawler |

## Source lifecycle policy

- A full valid snapshot enrolls eligible feeds and marks previously registered
  missing feeds inactive. Known comic feeds are marked inactive with that reason.
- Jobs retain links to every source that discovered them. They are eligible when
  at least one owner is active, or when explicitly independent/manual.
- A local pilot source file does **not** declare all other sources removed.
- Empty/invalid/unavailable lists do not change membership. A removal of over 20%
  of active non-comic sources needs a reviewed explicit override.
- Removing a source does not delete existing posts or search results. This is a
  reversible crawl-membership policy, not retroactive content moderation.
- Returning sources keep their history; their feed is checked again with validators
  cleared. Ordinary errors/backoff and historical budgets are not silently reset.
- Old installations have no trustworthy per-job provenance. Their jobs remain
  independent by default. This server's exact six pilot feed/root pairs were
  verified against the prior pilot notes and actual queue before explicit adoption.

## Verification and live state

- Full automated suite: **192 tests**, including disposable PostgreSQL 17 schemas
  and an opt-in real Elasticsearch test using an exact, uniquely named temporary
  index. No test writes to the live `pages` index.
- Source-aware benchmark: **300,000 jobs**, **10,000 sources**, **2,000 inactive**.
  Claiming eight eligible jobs on distinct hosts took **3.42 ms median / 6.95 ms
  maximum** over seven local runs. The first implementation took 141.93 ms median:
  an indexed scalar lookup and disabling JIT for claims removed unnecessary
  whole-ownership-table work/compilation. These are local SQL measurements, not
  end-to-end crawl speed or Neon throughput promises.
- A read-only scan of all **314,974** live Elasticsearch documents found a maximum
  legacy document version of **2**. The SQL revision sequence already exceeds that,
  so external revision ordering is compatible with this index. A mismatched restored
  database/index can still produce visible version conflicts and requires recovery.
- Schema and crawler-state backups: `backup/source-review.eQEqQa/`.
- Migrated the source registry and attached exactly **6** verified pilot feeds to
  **931** existing jobs. Before/after crawl-state digests (excluding only the new
  ownership flag) match. **314,974** saved pages, due work **0**, index backlog **0**.
- Both crawler/source timers remain uninstalled/inactive. No full-list import,
  worker run, live index mutation, or web-service restart occurred.

Run the full verification only with a disposable SQL database:

```sh
TEST_DATABASE_URL=postgresql://.../disposable_database \
TEST_ELASTICSEARCH_URL=http://127.0.0.1:9200 \
  .venv/bin/python -B -m unittest discover -s tests
```

The Elasticsearch test creates and removes only its own generated
`crawler-review-test-<uuid>` index. Database tests create/drop their own schemas.

## Deliberate limits, not hidden completion claims

- The six pilot histories remain budget-limited. A zero due count is not proof
  that every historical post has been discovered. Depth, response-size and
  lifetime per-site job limits remain protective controls.
- No JavaScript rendering, private/paywalled access, or recovery of completely
  unlinked URLs. No guessed whole-host crawl when a feed supplies no homepage.
- Utility-page filters are conservative. Mixed comic/text blogs and unusual
  static pages are not perfectly classified; short posts are intentionally retained.
- The [broader pilot and network pass](pilots/2026-09-07-broad-network.md) closes
  the checked-DNS/connect gap: sockets use only the validated numeric addresses,
  while urllib3 keeps the original hostname for TLS and HTTP. Every redirect and
  robots request uses this transport. This is application-level protection, not
  an OS/network sandbox; OS DNS and trickled response headers still lack a strict
  total deadline. The host's resolver/routing and installed Python libraries are trusted.
- The supported deployment is one crawler/index process on this host, enforced by
  a file lock. SQL leases additionally protect crawl jobs. This is not a distributed
  multi-host crawler or a promise of arbitrary-scale throughput.
- Source retirement still preserves previously saved/indexed content. The separate
  [old-index cleanup](audits/2026-09-07-index-cleanup.md) removes reviewed duplicates
  and non-articles, but is not a full recrawl or perfect content classifier.

The earlier 192-test pass was not evidence that extraction or robots handling had
no gaps; the follow-up above records concrete counterexamples and fixes. Reviewed
means tested within the documented boundary, not proven correct for every website.

## Other parts: not yet fully reviewed

- **Web backend/search:** prior reliability work covers connections, service
  lifecycle, error handling and several routes, with tests. A full review of query
  behavior, ranking, pagination, API limits and analytics remains separate. The
  live web process now includes the earlier backend changes following the runtime
  audit's tested restart. This does not complete the broader backend review.
- **Infrastructure/operations:** service/container locations and current crawler
  inactivity are verified. A full review of backups and restore drills, security,
  resource sizing, monitoring, deployment reproducibility and AWS retirement remains.
- **Later by user instruction:** automatic scheduling and the frontend.

Keep the ingestion queue/content/outbox in PostgreSQL for atomic completion and
durable recovery. Whether that PostgreSQL server should stay on Neon is a separate
cost/operations decision; this review does not establish that Neon is the cheapest
host or justify adding Redis/SQS alongside it.

Standards consulted for the small protocol additions:
[JSON Feed 1.1](https://www.jsonfeed.org/version/1.1/),
[Atom feed paging](https://www.rfc-editor.org/rfc/rfc5005.html), and
[Elasticsearch external versioning](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-index).
