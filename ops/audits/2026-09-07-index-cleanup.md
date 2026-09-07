# Existing PostgreSQL / Elasticsearch cleanup — 2026-09-07

## Scope and safeguards

Review the existing database and search index, clean demonstrated duplicates and
invalid results, remove leading title emoji, and prevent the reviewed duplicate
patterns from recurring. Keep `pages_old` and query history. Scheduling, frontend,
full recrawling, AWS retirement and moving away from Neon are outside this change.

The initial complete scan found 314,974 PostgreSQL articles and the same 314,974
Elasticsearch IDs. Every title, URL, article-text hash and publication date matched.
This was not a random sample. The changing last-checked timestamp is deliberately
not part of index-content equality: a 304 updates SQL without rewriting search.

Before any live cleanup, the entire public schema was dumped with PostgreSQL 17
and restored successfully into an isolated local PostgreSQL 17 database. That
copy is also the full-size cleanup rehearsal. PostgreSQL changes are transactional;
the local process lock and crawl-write advisory lock exclude crawler writes.
The applied manifest requires the baseline metadata and page count to match, and
production requires a successful rehearsal of the exact same manifest hash.

## Reviewed changes

- Merge 3,550 duplicate records: protocol/slash/tracking variants and reviewed
  site moves, supported by title/date/body comparisons or observed redirects.
- Remove 226 non-article records: 143 site-furniture bodies, 36 cookie notices,
  16 disclaimers, 14 placeholder bodies, seven subscriber notices, ten browser
  challenges. Exact reviewed whole-body hashes prevent these same bodies from
  being reinserted; posts merely discussing these subjects remain eligible.
- Keep 311,198 articles. Normalize 4,330 retained URLs and update 1,982 retained
  titles (including whitespace, missing titles and leading emoji). Preserve body
  text, publication dates and `original_url` for every retained ID. Where a proven
  copy has a useful title and its canonical row is untitled, retain that title.
- Standardize old fingerprints to whitespace-normalized SHA-256. Remove both
  redundant global fingerprint-unique indexes, replacing them with one ordinary
  fingerprint lookup. Identical text does not necessarily identify one article:
  recurring announcements, quotations and different authors can share words.
- Add `page_aliases`: old/proven alternative URLs resolve to the surviving numeric
  article ID, including after later text edits. Preserve `#fragment` distinctions
  in old records because some blogs publish separate posts under one HTML page.
- Add durable ES deletions to the existing outbox. Article merge/deletion and its
  search operation commit together; failures remain queued. Missing-document
  deletion retries succeed, while missing-index errors and version conflicts do not.
- Retire unused `feeds` (15,706 rows) and `skipped_urls` (6,836 rows), without
  `CASCADE`. Both remain in the restored/verified full backup. Keep current source,
  retry, backfill and domain-cooldown tables, even where currently empty.
- Remove redundant `pages_date_idx`; keep `pages_date_id_idx` for latest results,
  URL uniqueness, the primary key and the PostgreSQL full-text GIN index.
- Match the latest-post query's ordering to that composite index, explicitly
  using `date DESC NULLS LAST, id` in both pagination and final ordering. Before
  this fix the planner used a whole-table parallel scan/top-N sort. A live
  read-only comparison of selecting 20 IDs measured 85.827ms versus 0.167ms with
  the index scan. This is one query comparison, not an end-to-end latency promise.
  The ID tie-break also prevents inconsistent page boundaries for equal dates.
- Set the single-host `pages` index to zero replicas, keeping its existing single
  primary shard, mappings and ranking configuration. A replica cannot be assigned
  to the same node as its primary; an unassigned replica did not provide redundancy.
  This does not turn a single-host deployment into a highly available one.
- Set `index.gc_deletes=1h`: retain deleted-document versions beyond ordinary
  in-flight request/retry windows. This is finite protection, not permission to
  replay arbitrarily old batches after deletion or restore stale queue state.

The existing 8GB Elasticsearch heap / 12GB container cap were inspected and left
unchanged: this review did not establish sustained heap pressure or a defensible
better sizing target. No full index rebuild or force merge is required for this
small deletion percentage. Ordinary database maintenance follows the update.

## Tables after cleanup

| Table | Purpose / retention |
| --- | --- |
| `pages` | Current articles; authoritative content for search |
| `pages_old` | Protected archive; all 59,536 rows and original structure retained |
| `page_aliases` | Verified alternative article URLs; not another work queue |
| `query_logs` | Existing search history; retained, not exported to public reports |
| `domains` | Persisted host cooldowns; retained even when empty |
| `crawl_jobs` | URL work and retry history, including failed URLs |
| `crawl_sources` | Current upstream source membership and exclusions |
| `crawl_job_sources` | Which feeds own each crawl job |
| `crawl_sites` | Historical-discovery budgets and progress |
| `crawl_index_jobs` | Durable pending index/update/delete operations |

## Evidence and recovery

Private artifacts: `/home/debian/blog-search/backup/index-cleanup-20260907/`.
The directory is mode 0700; its database dump contains private query history.

- `public-before.dump`: 1,070,693,987 bytes, SHA-256
  `9eb64f32f40047c7f79b9346e6a9389017611e14153b5f4fb96c6c8fc761755d`.
- ES repository `blogsearch_local`, pages-only snapshot
  `before-index-cleanup-20260907-070627`, successful before mutation.
- `database.jsonl.gz` and `elasticsearch.jsonl.gz`: immutable baseline metadata
  and complete article hashes; `index-differences.json`: initial zero differences.
- `cleanup-plan.json`: exact source/target IDs, evidence, removals and metadata
  repairs; `alias-verification.json` and `extra-alias-verification.json`: bounded
  public checks with normal robots, TLS, destination protection and five-second
  host spacing. `reviewed-moves.json` records manual same-site/path-move decisions
  separately from network results; identical historical text and host are required.
- `rehearsal.json`, `applied-live.json`, `verified-rehearsal.json`, and
  `verified-live.json`: execution/verification receipts. A missing receipt is not
  proof of completion; consult the final verification section below.

For selective recovery, restore the dump with PostgreSQL 17 into a separate empty
database (as rehearsed), inspect the manifest's removed IDs/tables, and restore
only the intended records. Do not blindly overwrite live data. If restoring the
whole system, coordinate database and ES snapshots/revisions; an old SQL sequence
must not be reused against newer external ES versions. These local backups are
recoverable maintenance backups, not protection against losing this entire host.

## Verification boundary

The full automated suite passed 294 tests, including real PostgreSQL transactions in
isolated schemas and real Elasticsearch delivery/deletion tests in disposable
indexes. Cases include concurrent completion, known aliases after edits, identical
text on unrelated posts, redirect merges, 304 alias lookup, rollback, stale writes,
idempotent deletions, Unicode title handling and distinct fragment posts.
The final latest-post query change adds another regression; all 37 focused web
backend tests pass after that change. Its real query plan was measured on live
data. A restricted-sandbox web test run stalled; the same focused tests completed
in 0.146 seconds under the permissions used by the passing full suite. That
stalled test process was stopped; no production process was terminated by it.

Final verification compares every retained PostgreSQL row to the baseline, hashing
text server-side to avoid retransmitting another ~2GB from Neon. It then scans and
hashes every Elasticsearch article. Archive row-count/content checksums must match;
query history must not shrink, and the live outbox must be empty.

Ten ambiguous protocol/slash groups remain untouched because saved versions differ
and live endpoints could not safely establish identity. Identical text on distinct
dated posts, memorials, papers or quotations is deliberately not globally collapsed.
Some remaining repeated bodies look like old related-post excerpts; the complete
duplicate report preserves those candidates, but uncertain content is not silently
deleted. This is not a semantic audit of every paragraph, a full re-extraction, or
a guarantee that every website's future duplicate pattern can be recognized.

Normal crawler discovery already excludes known comic feeds, but this cleanup
does not infer historical post provenance or delete whole domains as comics.
No schedule is enabled by these changes.

## References

- [PostgreSQL unique constraints and their indexes](https://www.postgresql.org/docs/18/ddl-constraints.html)
- [Elasticsearch single-node unassigned replicas](https://www.elastic.co/docs/troubleshoot/elasticsearch/red-yellow-cluster-status)
- [Elasticsearch shard sizing](https://www.elastic.co/docs/deploy-manage/production-guidance/optimize-performance/size-shards)
- [Elasticsearch bulk/delete versioning](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk)
- [Elasticsearch deletion-version retention](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-delete)

## Final verification

Completed on 2026-09-07:

- Rehearsal and live cleanup use manifest SHA-256
  `e4d76bbfa2fdbf9df6decf12814fd724b01a730c7fd96bb0359a663021529078`.
- Exactly **311,198 retained PostgreSQL rows** matched their expected IDs,
  titles, URLs, provenance, publication dates and entire article-text hashes.
  All **311,198 Elasticsearch documents** then matched the same expected content.
  No missing/extra IDs or content differences remain between the two stores.
- **59,536 `pages_old` rows**, before/after content checksum
  `65533e97e61b2519a83063f67cf0dbe3`, unchanged in rehearsal and production.
- **16,388 query log rows** retained live (one legitimate addition since backup).
- **7,884 aliases**; no alias/canonical overlap. No empty titles or NULL active
  full-text vectors. **310,349 legacy fingerprints** standardized.
- **10,067 index operations delivered**, no failures and zero remaining backlog.
  Elasticsearch `pages` health is **green**.
- Ordinary `VACUUM (ANALYZE, TRUNCATE FALSE)` completed for `pages`,
  `page_aliases`, and `crawl_index_jobs`; no archive vacuum/rewrite or VACUUM FULL.
- Disposable restored database/container and its exact scratch volume removed;
  roughly **7GB reclaimed**, about **66GB free** on the host. Backups retained.
- Lockfile/environment dry-run: 45 packages checked, no package changes needed.
- Web service restarted successfully at **08:21:43 UTC** with the tested query
  change. Homepage, latest-post pages 1/2 and search API all returned HTTP 200;
  the search smoke query returned six results from 840 matches. Full Elasticsearch
  cluster health is green. `git diff --check` is clean.
- Crawler/source services and timers remain inactive/uninstalled. No crawl or
  source import ran as part of live cleanup or verification.
