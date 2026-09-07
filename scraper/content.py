"""Turn feed, sitemap, and HTML responses into crawl jobs and searchable pages."""

import gzip
import json
import re
from datetime import date
from io import BytesIO
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import fastfeedparser
import trafilatura
from lxml import etree, html

from scraper.fetching import FetchError, normalize_url, same_site
from scraper.records import clean_title, content_fingerprint, nonarticle_reason


SMALLWEB_URL = "https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"
SMALLCOMIC_URL = "https://raw.githubusercontent.com/kagisearch/smallweb/main/smallcomic.txt"
FEED_PAGE_DEPTH = 20
ARCHIVE_PATH = re.compile(r"/(?:archives?|tags?|categor(?:y|ies)|page)(?:/|$)", re.I)
ASSET_PATH = re.compile(
    r"\.(?:jpg|jpeg|png|gif|svg|webp|ico|pdf|zip|gz|mp[34]|woff2?|css|js|xml|json|rss|atom)$", re.I
)


def job(kind, url, *, priority=10, root=None, depth=0):
    url = normalize_url(url)
    if not url:
        return None
    return {
        "kind": kind, "url": url, "host": urlsplit(url).hostname,
        "priority": priority, "payload": {"root": root or url, "depth": depth},
    }


def source_jobs(text, excluded=()):
    """The upstream file contains feed URLs, optionally followed by comments."""
    urls = {normalize_url(line.split("#", 1)[0].strip()) for line in text.splitlines()}
    excluded = {normalize_url(url) for url in excluded}
    return [job("feed", url, priority=100) for url in sorted(urls - {None} - excluded)]


def in_scope(url, root):
    if not same_site(url, root):
        return False
    # Preserve path scoping for blogs hosted under a shared site's subdirectory.
    base_path = urlsplit(root).path.rstrip("/")
    return not base_path or urlsplit(url).path == base_path or urlsplit(url).path.startswith(base_path + "/")


def is_archive_url(url, root=None):
    """Recognize listing URLs consistently in sitemaps and HTML navigation."""
    parts = urlsplit(url)
    if ARCHIVE_PATH.search(parts.path):
        return True
    relative_path = parts.path
    if root and same_site(url, root):
        base = urlsplit(root)
        if parts.path.rstrip("/") == base.path.rstrip("/") and parts.query == base.query:
            return True
        prefix = base.path.rstrip("/") + "/"
        if parts.path.startswith(prefix):
            relative_path = "/" + parts.path[len(prefix):]
    # /blog/post/ can be a listing; /blog/2004/post/ is an ordinary post slug.
    return re.fullmatch(r"/(?:posts?|blog)/?", relative_path, re.I) is not None or re.fullmatch(
        r'/(?:19|20)\d{2}/(?:0?[1-9]|1[0-2])/?', relative_path
    ) is not None


def is_utility_url(url, root=None):
    """Conservative navigation filter; do not reject dated posts or short text."""
    parts = urlsplit(url)
    path = unquote(parts.path).lower()
    if ASSET_PATH.search(path) or re.search(r"/(?:wp-admin|wp-login|login|logout|search)(?:[/.]|$)", path):
        return True
    if {"s", "replytocom"} & parse_qs(parts.query).keys():
        return True
    if root and same_site(url, root):
        prefix = urlsplit(root).path.rstrip("/") + "/"
        if path.startswith(prefix.lower()):
            path = path[len(prefix):]
    path = re.sub(r"\.(?:html?|php)$", "", path.strip("/"))
    return path in {"about", "about-me", "contact", "contact-me", "privacy", "privacy-policy",
                    "terms", "terms-of-service", "subscribe", "unsubscribe", "cookie-policy"}


def feed_jobs(body, feed_url, *, root=None, depth=0, historical=False):
    parsed = fastfeedparser.parse(body, include_content=False, include_tags=False,
                                  include_media=False, include_enclosures=False)
    homepage_link = parsed.feed.get("link")
    homepage = normalize_url(urljoin(feed_url, homepage_link)) if homepage_link else None
    root = root or homepage or feed_url
    links = {}
    for entry in parsed.entries:
        entry_link = entry.get("link")
        url = normalize_url(urljoin(feed_url, entry_link)) if entry_link else None
        if url and url != feed_url and not is_utility_url(url, root):
            # Feed entries can legitimately link to another hostname. Only archive
            # traversal, below, is restricted to the blog's own scope.
            links[("page", url)] = job("page", url, priority=10 if historical else 90, root=root)
    if depth < FEED_PAGE_DEPTH:
        pagination_links = list(parsed.feed.get("links") or [])
        # The installed parser preserves JSON Feed entries, but omits next_url.
        if body.lstrip().startswith(b'{'):
            next_url = json.loads(body).get('next_url')
            if isinstance(next_url, str):
                pagination_links.append({'rel': 'next', 'href': next_url})
        for link in pagination_links:
            # Follow declared pagination only; guessing page numbers can loop forever.
            if link.get("rel") not in {"next", "prev-archive"} or not link.get("href"):
                continue
            url = normalize_url(urljoin(feed_url, link["href"]))
            if url and url != feed_url and same_site(url, feed_url):
                links[("feed_page", url)] = job("feed_page", url, priority=40, root=root, depth=depth + 1)
    if homepage and not historical:
        links[("archive", homepage)] = job("archive", homepage, priority=20, root=homepage)
        sitemap = urljoin(homepage, "/sitemap.xml")
        links[("sitemap", sitemap)] = job("sitemap", sitemap, priority=30, root=homepage)
    return list(links.values()), homepage


def sitemap_jobs(body, sitemap_url, root, depth=0, max_depth=8):
    if body.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
            body = compressed.read(5_000_001)
        if len(body) > 5_000_000:
            raise ValueError("Expanded sitemap exceeds size limit")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    tree = etree.fromstring(body, parser=parser)
    if tree.getroottree().docinfo.doctype:
        raise ValueError("Sitemap DTDs are not supported")
    name = etree.QName(tree).localname
    if name not in {"sitemapindex", "urlset"}:
        raise ValueError("Response is not a sitemap")
    kind = "sitemap" if name == "sitemapindex" else "page"
    if kind == "sitemap" and depth >= max_depth:
        return []
    result = {}
    for loc in tree.xpath('./*[local-name()="sitemap" or local-name()="url"]/*[local-name()="loc"]'):
        url = normalize_url(urljoin(sitemap_url, loc.text or ""))
        if not url or (kind == "page" and (not in_scope(url, root) or is_utility_url(url, root))):
            continue
        # Sitemap indexes occasionally delegate to a CDN; every fetch still goes
        # through public-address checks and robots rules.
        target_kind = "archive" if kind == "page" and is_archive_url(url, root) else kind
        priority = {"sitemap": 30, "archive": 20, "page": 10}[target_kind]
        # XML nesting and HTML link traversal have independent depth limits.
        result[url] = job(target_kind, url, priority=priority, root=root,
                          depth=depth + 1 if kind == "sitemap" else 0)
    return list(result.values())


def archive_jobs(body, page_url, root, depth=0, max_depth=5, *, listing=False):
    if depth >= max_depth:
        return []
    try:
        tree = html.fromstring(body)
    except etree.ParserError:
        return []  # Empty/whitespace-only HTML is not a transient network error.
    result = {}
    for anchor in tree.xpath('//a[@href] | //head/link[@href]'):
        relations = set((anchor.get("rel") or "").lower().split())
        pagination = bool({"next", "prev"} & relations)
        if anchor.tag == "link" and not pagination:
            continue
        url = normalize_url(urljoin(page_url, anchor.get("href")))
        if not url or url == page_url or not in_scope(url, root):
            continue
        if is_utility_url(url, root):
            continue
        label = " ".join(anchor.itertext()).strip().lower()
        # On individual posts, rel=next can mean the next article, not a listing.
        current_listing = listing or is_archive_url(page_url, root)
        archive = is_archive_url(url, root) or (pagination and current_listing)
        archive = archive or label in {"older posts", "archive", "archives", "all posts"}
        archive = archive or (current_listing and label in {"older", "next", "next page"})
        kind = "archive" if archive else "page"
        # Head pagination and body navigation can point to the same URL.
        if kind == "page" and ("archive", url) in result:
            continue
        if kind == "archive":
            result.pop(("page", url), None)
        result[(kind, url)] = job(kind, url, root=root, depth=depth + 1, priority=20 if archive else 10)
    return list(result.values())


class PageRejected(ValueError):
    """A fetched page intentionally excluded from the search index."""


def _noindex(value):
    applicable = True
    for part in value.lower().split(','):
        if ':' in part:
            agent, part = part.split(':', 1)
            applicable = agent.strip() in {'*', 'blogsearchbot'}
        if applicable and {'noindex', 'none'} & set(part.split()):
            return True
    return False


def _clean_text(value):
    # PostgreSQL cannot store NUL. Keep line breaks, tabs, accents and code spaces.
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value).strip()


def extract_page(body, url, *, content_type='', robots_header=''):
    """Extract locally from fetched bytes; never ask Trafilatura to download URLs."""
    if _noindex(robots_header):
        raise PageRejected('noindex')
    if content_type.partition(';')[0].strip().lower() == 'text/plain':
        charset = re.search(r'charset=["\']?([^;\s"\']+)', content_type, re.I)
        try:
            text = body.decode(charset.group(1) if charset else 'utf-8', errors='replace')
        except LookupError:
            text = body.decode('utf-8', errors='replace')
        return _page_record(text, urlsplit(url).hostname, None, url)
    if not body.strip():
        return None
    try:
        charset = re.search(r'charset=["\']?([^;\s"\']+)', content_type, re.I)
        encoding = charset.group(1) if charset else None
        if not encoding:
            try:
                body.decode('utf-8')
                encoding = 'utf-8'
            except UnicodeDecodeError:
                pass  # Let libxml read an HTML charset declaration.
        tree = html.fromstring(body, parser=html.HTMLParser(encoding=encoding, no_network=True))
    except LookupError:
        tree = html.fromstring(body, parser=html.HTMLParser(encoding='utf-8', no_network=True))
    except (etree.ParserError, ValueError):
        return None
    for meta in tree.xpath('//meta[@name][@content]'):
        if meta.get('name').lower() in {'robots', 'blogsearchbot'} and _noindex(meta.get('content')):
            raise PageRejected('noindex')
    title = ' '.join(tree.xpath('//title/text()')).strip().lower()
    heading = ' '.join(tree.xpath('//h1//text()')).strip().lower()
    # Exact error headings, not keywords in articles about errors or web security.
    error_heading = re.compile(r'(?:(?:404|410)\s*[:–—-]?\s*)?(?:page not found|not found|page gone)[.!]?')
    if error_heading.fullmatch(title) or error_heading.fullmatch(heading):
        raise PageRejected('soft_404')
    if title in {'just a moment...', 'attention required! | cloudflare', 'verify you are human'} or (
        heading.startswith('checking your browser before accessing')
    ):
        raise FetchError('Browser challenge instead of article')
    # Remove obvious furniture BEFORE extraction, so fallback algorithms cannot
    # reintroduce it. Match exact comment container names, not post slug keywords.
    for node in tree.xpath('//nav | //footer | //script | //style | //noscript | //form | '
                           '//*[@role="navigation"] | //*[@id="comments"] | '
                           '//*[@id="disqus_thread"] | //*[@id="respond"] | //*[@id="post-notes"] | '
                           '//ol[contains(concat(" ", normalize-space(@class), " "), " notes ")]'
                           '[li[contains(concat(" ", normalize-space(@class), " "), " note ")]] | '
                           '//*[contains(concat(" ", normalize-space(@class), " "), " comments-area ")]'):
        if node is tree:
            return None
        if node.getparent() is not None:
            node.drop_tree()
    # A disclosure widget is part of an article, not an article boundary.
    # Trafilatura's precision path can otherwise select only the first <details>
    # block (e.g. setup code) and silently drop the rest of a Quarto post.
    for node in tree.xpath('//details'):
        node.tag = 'div'
    article_body = tree.find('body') if tree.tag == 'html' else tree
    if article_body is None or not ''.join(article_body.itertext()).strip():
        return None
    document = trafilatura.bare_extraction(
        tree, url=url, with_metadata=True, include_comments=False,
        include_tables=True, favor_precision=True,
    )
    if document is None:
        return None
    # Honor an unambiguous, explicitly marked article body. Blogger commonly
    # mixes <p> and bare <div> paragraphs; whole-page precision extraction can
    # retain only the <p>s. Keep metadata from the complete document, and run the
    # body-only extractor where navigation/comments have already been removed.
    article_bodies = tree.xpath('//*[contains(concat(" ", normalize-space(@itemprop), " "), " articleBody ")]')
    if len(article_bodies) == 1:
        article = trafilatura.bare_extraction(
            article_bodies[0], url=url, include_comments=False, include_tables=True, favor_recall=True,
        )
        if article is not None and article.text:
            document.text = article.text
    return _page_record(document.text or '', document.title, document.date, url)


def _page_record(text, title, published_date, url):
    text = _clean_text(text)
    if not text:
        return None
    if reason := nonarticle_reason(text):
        raise PageRejected(reason)
    # Keep short personal posts. Length alone is not a useful quality judgment.
    published = None
    if published_date:
        try:
            published = date.fromisoformat(str(published_date)[:10]).isoformat()
        except ValueError:
            pass
    return {
        "title": clean_title(title, url),
        "url": url,
        "fingerprint": content_fingerprint(text),
        "date": published,
        "text": text,
    }
