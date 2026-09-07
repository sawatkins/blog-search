"""Turn feed, sitemap, and HTML responses into crawl jobs and searchable pages."""

import hashlib
import gzip
import re
from datetime import date
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import fastfeedparser
import trafilatura
from lxml import etree, html

from scraper.fetching import normalize_url, same_site


SMALLWEB_URL = "https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"
ARCHIVE_PATH = re.compile(r"/(?:archives?|tags?|categor(?:y|ies)|page)(?:/|$)|/posts/?$", re.I)
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


def source_jobs(text):
    """The upstream file contains feed URLs, optionally followed by comments."""
    urls = {normalize_url(line.split("#", 1)[0].strip()) for line in text.splitlines()}
    return [job("feed", url, priority=100) for url in sorted(urls - {None})]


def in_scope(url, root):
    if not same_site(url, root):
        return False
    # Preserve path scoping for blogs hosted under a shared site's subdirectory.
    base_path = urlsplit(root).path.rstrip("/")
    return not base_path or urlsplit(url).path == base_path or urlsplit(url).path.startswith(base_path + "/")


def feed_jobs(body, feed_url):
    parsed = fastfeedparser.parse(body)
    homepage_link = parsed.feed.get("link")
    homepage = normalize_url(urljoin(feed_url, homepage_link)) if homepage_link else None
    links = {}
    for entry in parsed.entries:
        url = normalize_url(urljoin(feed_url, entry.get("link") or ""))
        if url and url != feed_url:
            # Feed entries can legitimately link to another hostname. Only archive
            # traversal, below, is restricted to the blog's own scope.
            links[("page", url)] = job("page", url, priority=90, root=homepage or url)
    if homepage:
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
        if not url or (kind == "page" and not in_scope(url, root)):
            continue
        # Sitemap indexes occasionally delegate to a CDN; every fetch still goes
        # through public-address checks and robots rules.
        result[url] = job(kind, url, priority=30 if kind == "sitemap" else 10, root=root, depth=depth + 1)
    return list(result.values())


def archive_jobs(body, page_url, root, depth=0, max_depth=5):
    if depth >= max_depth:
        return []
    tree = html.fromstring(body)
    result = {}
    for anchor in tree.xpath('//a[@href]'):
        url = normalize_url(urljoin(page_url, anchor.get("href")))
        if not url or url == page_url or not in_scope(url, root):
            continue
        path = urlsplit(url).path
        if ASSET_PATH.search(path) or re.search(r"/(?:wp-admin|wp-login|login|logout|search)(?:[/.]|$)", path):
            continue
        label = " ".join(anchor.itertext()).strip().lower()
        archive = bool(ARCHIVE_PATH.search(path)) or bool({"next", "prev"} & set((anchor.get("rel") or "").split()))
        archive = archive or label in {"older", "older posts", "next", "next page", "archive", "archives", "all posts"}
        kind = "archive" if archive else "page"
        result[(kind, url)] = job(kind, url, root=root, depth=depth + 1, priority=20 if archive else 10)
    return list(result.values())


def extract_page(body, url):
    """Use the public extraction API, including its own fallback algorithms."""
    document = trafilatura.bare_extraction(
        body, url=url, with_metadata=True, include_comments=False,
        include_tables=True, favor_recall=True,
    )
    if document is None:
        return None
    text = (document.text or "").strip()
    if not text:
        return None
    # Keep short personal posts. Length alone is not a useful quality judgment.
    published = None
    if document.date:
        try:
            published = date.fromisoformat(str(document.date)[:10]).isoformat()
        except ValueError:
            pass
    return {
        "title": document.title or urlsplit(url).hostname,
        "url": url,
        "fingerprint": hashlib.sha256(" ".join(text.split()).encode()).hexdigest(),
        "date": published,
        "text": text,
    }
