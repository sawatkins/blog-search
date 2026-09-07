# Broader crawler pilot and network protection — 2026-09-07

## Scope and result

Completed a bounded 48-feed live experiment, followed by offline replay through
the corrected worker, real PostgreSQL and Elasticsearch. No production crawl,
source-list import, old-index cleanup, reindex, scheduler activation or web restart.
The only production database change was the additive nullable
`crawl_jobs.resolved_url` column. Existing article and queue rows were not rewritten.

- 48 feeds: 21 seeded random choices plus three each from FeedBurner, Blogger,
  WordPress.com, Bear Blog, Micro.blog, JSON feed paths, subdirectory blogs,
  Ghost and Tumblr. Selection seed: `20260907`. All came from the current Small Web
  snapshot, excluding normalized feed URLs in Small Comic; 40,604 eligible feeds.
- 45 feeds parsed: 43 XML and two JSON. `exeami.com` and `mcknight.io` disallowed
  the requested feeds under robots policy. `yeikoff.xyz` failed TLS hostname
  verification; a follow-up confirmed the certificate mismatch. No bypasses.
- Two recent entries per readable feed were seeded, together with discovered
  archive/sitemap entry points. Historical jobs were capped at 12 per root.
  This is a deliberately bounded coverage experiment, not a production full sync.
- 612 live worker executions across 609 unique jobs; 388 documents saved/indexed
  before extraction/filter fixes. All saved URL/title/date/text values matched ES.
  The HTTP recorder observed 38 redirect responses, including robots redirects;
  33 complete fetches ended at a different URL.
- Corrected offline replay: 609 executions, **383 saved/indexed documents**, zero
  missing response fixtures, zero SQL/ES content mismatches, zero pending index
  writes. The five-document difference is five identified archive/listing pages,
  omitted in the fresh test schema, not removed from the production corpus.
- 102 retained URLs were absent from the sampled feed-entry URLs, across 26 roots.
  These are URLs, not a claim that all are individual posts or older than the feed.
  A concrete historical example is Shellen's July 2000 post at
  `https://www.shellen.com/2000/07/10/chronic-halitosis/`.

42 of 45 live historical budgets reached their caps. Three roots did not; zero
due jobs still does not establish complete histories, because discovery/depth
limits and unavailable URLs remain. The live snapshot included two transient
empty-HTML parser failures and one malformed sitemap, with retries bounded by
the existing queue policy. Empty HTML is now treated as empty discovery; the
malformed sitemap remains a visible bounded failure.

## Network boundary

The fetcher now resolves each request target once, rejects the whole answer set
if any address is non-public, and passes the checked numeric socket addresses to
a narrowly scoped Requests adapter. The connection's TCP dialing uses those
addresses directly: **no second hostname lookup between validation and connect**.
IPv4/IPv6 fallback stays within the checked set and shares the connection budget.
Known IPv6 translation/tunnel ranges are rejected to prevent embedded private-IPv4
destinations. This restriction intentionally also excludes public addresses in
those transition ranges.

Only dialing is customized. Requests/urllib3 still supplies the original Host
header, TLS SNI, certificate-chain validation and hostname matching. The adapter
is private to each request; global resolver behavior and other app connections
are unchanged. Environment proxies and `.netrc` remain disabled. Every robots
request and redirect hop follows the same transport and policy checks.

Real local HTTP/TLS fixtures exercise the adapter (not a mocked HTTP send): a
second DNS answer would be loopback, but no second lookup occurs; SNI/Host remain
correct; a mismatched certificate fails before HTTP. Other tests cover mixed
public/private DNS answers, IPv4/IPv6/private/metadata redirects, redirect-host
robots rules, five-hop bounds, byte limits and per-host spacing.

This closes the application-level DNS-rebinding gap, **not** a kernel/network
sandbox or a guarantee against compromised dependencies/resolver/routing. No
machine-wide firewall changes were made. The OS DNS resolver and trickled response
headers still lack a strict overall timeout; existing socket timeouts, body
watchdog and request-chain budget remain. No browser/JavaScript execution is added.

## Redirect identity and discovery

- Feed/tracking-host links can redirect to another public blog. HTTP/HTTPS,
  relative redirects, adding/removing a trailing slash and Tumblr slug redirects
  remain supported. Trafilatura receives already-downloaded HTML and the final
  URL; it never performs its own redirect/download handling.
- Original input URLs remain durable jobs. Saved articles use the **final URL**,
  so two jobs reaching that URL update one article, including when content changes.
  Separate aliases may still each make a future recheck; they are not blindly
  collapsed or permanently cached as redirects that might later change.
- HTTP/HTTPS and slash variants are **not assumed equivalent without evidence**.
  Some servers serve different resources there. The existing global text
  fingerprint rule remains unchanged; its identical-text identity limitation is
  outside this pass and no legacy pages/fingerprints were rewritten.
- `resolved_url` associates cache validators with the URL that issued them.
  Rechecks follow the original link, sending validators only upon reaching the
  saved destination. A changed destination receives an unconditional request.
  Legacy jobs without this association fetch once without their old validators.
  A 304 refreshes the stored final article's timestamp, not the redirect URL.
- A demonstrated homepage redirect can move historical discovery scope. On the
  captured `grantseltzer.github.io` → `grant.pizza` homepage, the old scope yielded
  zero links; the corrected worker yields 38 links plus a sitemap seed. Child jobs
  keep the old budget root and source ownership, using `payload.scope_root` to
  remember the new hostname/path. Real SQL tests verify a four-job budget remains
  one four-job budget. Redirecting a non-root post/archive/CDN sitemap does not
  authorize crawling a whole unrelated site.

## Extraction and classification fixes

Compared identical downloaded bodies, without repeatedly requesting public sites:

| Pattern | Before | After |
| --- | --- | --- |
| Blogger mixed paragraph/div article (Balashon) | 372 characters, most paragraphs missing | 4,841 characters, missing paragraphs recovered |
| Quarto data-analysis post | 454 characters of setup code only | 23,758 characters including analysis and code |
| Tumblr knitting post | 1,725 characters including likes/reblogs | 38 characters of actual post text |

The fixes are semantic and localized:

- Treat `<details>` disclosures as content containers so precision extraction
  does not stop at an opening setup-code widget.
- For exactly one explicitly marked `articleBody`, extract within that body
  using recall mode, after pruning comments/navigation; retain metadata from the
  full document. This recovers link-heavy/mixed-markup paragraphs without turning
  on whole-page recall. An initial default-mode body-only trial still dropped
  linked paragraphs; the final recall-mode comparison recovered those.
- Prune Tumblr post-note containers and reaction lists, not ordinary personal
  lists called “notes.” Short personal posts remain valid.
- Traverse root-relative `/blog` listings and year/month archive URLs as discovery,
  rather than indexing them as posts. Dated article paths remain articles.
- Empty/whitespace-only historical HTML yields no links instead of a parser retry.

The final replay changed text in 31 records and excluded five listing pages.
Inspection included sampled article snippets, all changed-record sizes and
targeted word-level diffs, especially shortened and heavily expanded results.
This is not a blinded precision/recall score or proof of perfect extraction.
Minor heading/caption loss remains possible, as do unusual layouts. Some public
static/personal pages and photo captions remain; excluding a comic feed list is
not a perfect classifier of every mixed art/text blog. Publication dates remain
best-effort. No new length threshold, external extraction API or browser service.

## Failure memory

Terminal URL failures remain in `crawl_jobs` with `status='failed'` and an error;
ordinary rediscovery does not reactivate them. Dead 404/410 feeds retain their
monthly recheck policy. Host-wide cooldowns are stored in
`domains.next_allowed_scrape`, with local robots policy cached separately.
There is no permanent “this entire domain can never recover” blacklist: DNS/TLS
outages are not evidence that every URL on a domain is permanently gone.

## Verification, retained evidence and production boundary

- Final full regression suite: **242 tests**, using isolated PostgreSQL schemas
  and uniquely named temporary Elasticsearch indexes.
- Live PostgreSQL pages **314,974** before/after; ES documents **314,974**;
  production crawl jobs **931**, active managed feeds **6**, pending index writes
  **0**. Search API returned HTTP 200 and the web service remained active.
- Crawler/source timers remain uninstalled and inactive. No old SQS changes.
- Reproduction tools: `run_broad_pilot.py` and `replay_broad_pilot.py` here.
  They require `TEST_DATABASE_URL` pointing to a disposable localhost database,
  never load `.env`, and use unique schemas and temporary search indexes.
  Example (from the repository root, with the test DSN already set):

  ```sh
  PYTHONPATH=. .venv/bin/python ops/pilots/run_broad_pilot.py --output /tmp/new-pilot-directory --minutes 10
  PYTHONPATH=. .venv/bin/python ops/pilots/replay_broad_pilot.py /tmp/new-pilot-directory
  ```

- Public snapshots, selection, captured responses, before/after article records,
  outcomes and comparisons are retained in ignored
  `/home/debian/blog-search/backup/broad-pilot-20260907/` (about 63 MB).
  Full public article bodies are not committed to Git. Test search indexes are
  deleted by the harness; the disposable database container is removed after review.

References: [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html),
[urllib3 TLS/hostname behavior](https://urllib3.readthedocs.io/en/stable/advanced-usage.html#custom-sni-hostname),
[Kagi Small Web source](https://github.com/kagisearch/smallweb).
