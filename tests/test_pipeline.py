"""Exercise discovery, extraction, retries and indexing together with real SQL."""

import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from scraper.content import job, source_jobs
from scraper.fetching import FetchError, FetchResult, Fetcher
from scraper.storage import Store
from scraper.worker import CrawlPaused, Worker, flush_index


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PipelineTests(unittest.TestCase):
    def database(self):
        schema = "test_pipeline_" + uuid4().hex
        dsn = make_dsn(os.environ["TEST_DATABASE_URL"], options="-csearch_path=" + schema)
        connection = psycopg2.connect(dsn)
        connection.autocommit = True
        self.addCleanup(connection.close)
        cursor = connection.cursor()
        self.addCleanup(cursor.close)
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        self.addCleanup(lambda: cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))))
        return dsn, cursor

    def test_connection_outage_stops_then_remaining_jobs_resume_without_loss(self):
        dsn, cursor = self.database()
        tasks = [job('page', f'https://site-{i}.example.org/post', priority=90) for i in range(100)]
        fetcher = Mock()
        fetcher.fetch.side_effect = FetchError('Connection failed', failure_kind='network_error')
        with Store(dsn) as store:
            store.migrate()
            store.enqueue(tasks)
            with self.assertRaises(CrawlPaused):
                Worker(store, fetcher, workers=1).run()
            self.assertEqual(fetcher.fetch.call_count, 5)
            cursor.execute('SELECT status, attempts, COUNT(*) FROM crawl_jobs GROUP BY status, attempts')
            self.assertEqual({(status, attempts): count for status, attempts, count in cursor.fetchall()},
                             {('pending', 0): 96, ('pending', 1): 4})
            cursor.execute('SELECT COUNT(*) FROM crawl_jobs WHERE lease_token IS NOT NULL OR lease_until IS NOT NULL')
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute("UPDATE crawl_jobs SET due_at = NOW() - INTERVAL '1 second'")
            fetcher.fetch.side_effect = lambda url, **_: FetchResult(url, b'', 304)
            self.assertEqual(Worker(store, fetcher).run(), 100)
            cursor.execute('SELECT COUNT(*), MIN(attempts), MAX(attempts) FROM crawl_jobs')
            self.assertEqual(cursor.fetchone(), (100, 0, 0))

    def test_internal_error_does_not_exhaust_last_url_attempt(self):
        dsn, cursor = self.database()
        fetcher = Mock()
        fetcher.fetch.side_effect = RuntimeError('local bug')
        with Store(dsn) as store:
            store.migrate()
            store.enqueue([job('page', 'https://site.example.org/post')])
            cursor.execute('UPDATE crawl_jobs SET attempts = 7')
            with self.assertRaises(CrawlPaused):
                Worker(store, fetcher).run()
            cursor.execute('SELECT status, attempts, lease_token FROM crawl_jobs')
            self.assertEqual(cursor.fetchone(), ('pending', 7, None))

    def test_feed_redirect_variants_share_one_saved_article_and_recheck_final_url(self):
        import io
        import socket
        import requests
        from urllib3.response import HTTPResponse

        dsn, cursor = self.database()
        feed_url = 'https://feeds.example.org/rss'
        final_url = 'https://blog.example.org/post/'
        feed = b'''<rss version="2.0"><channel><title>Blog</title>
            <item><link>http://links.example.org/track/1</link></item>
            <item><link>https://blog.example.org/post</link></item>
            </channel></rss>'''
        article = b'<html><head><title>A morning walk</title></head><body><article><p>The sun rose above the river as we crossed the quiet bridge.</p></article></body></html>'
        requests_seen = []

        def send(request, **kwargs):
            requests_seen.append(request)
            response = requests.Response()
            response.status_code = 200
            body = b''
            if request.url.endswith('/robots.txt'):
                body = b'User-agent: *\nAllow: /'
            elif request.url == feed_url:
                body = feed
            elif request.url == 'http://links.example.org/track/1':
                response.status_code = 302
                response.headers['Location'] = 'http://blog.example.org/post'
            elif request.url == 'http://blog.example.org/post':
                response.status_code = 301
                response.headers['Location'] = 'https://blog.example.org/post'
            elif request.url == 'https://blog.example.org/post':
                response.status_code = 308
                response.headers['Location'] = '/post/'
            elif request.url == final_url:
                response.headers['ETag'] = '"article-1"'
                response.headers['Content-Type'] = 'text/html'
                if request.headers.get('If-None-Match') == '"article-1"':
                    response.status_code = 304
                else:
                    body = article
            else:
                raise AssertionError('Unexpected request: ' + request.url)
            response.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False, headers=response.headers)
            return response

        def resolve(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

        fetcher = Fetcher(delay=0)
        self.addCleanup(fetcher.close)
        with Store(dsn) as store, patch('socket.getaddrinfo', side_effect=resolve), \
                patch('requests.adapters.HTTPAdapter.send', side_effect=send):
            store.migrate()
            store.enqueue(source_jobs(feed_url))
            worker = Worker(store, fetcher)
            self.assertEqual(worker.run(), 3)
            cursor.execute('SELECT url, text FROM pages')
            self.assertEqual(cursor.fetchall(), [(final_url, 'The sun rose above the river as we crossed the quiet bridge.')])
            self.assertEqual(store.status()['outbox'], 1)
            cursor.execute("SELECT DISTINCT resolved_url FROM crawl_jobs WHERE kind = 'page'")
            self.assertEqual(cursor.fetchall(), [(final_url,)])
            cursor.execute("UPDATE pages SET scraped_on_date = '2000-01-01'")
            cursor.execute("UPDATE crawl_jobs SET due_at = NOW() WHERE kind = 'page'")
            requests_seen.clear()
            self.assertEqual(worker.run(), 2)
            self.assertEqual([r.url for r in requests_seen if r.headers.get('If-None-Match')], [final_url, final_url])
            cursor.execute("SELECT COUNT(*), MIN(scraped_on_date) > '2000-01-02' FROM pages")
            self.assertEqual(cursor.fetchone(), (1, True))

    def test_moved_blog_discovery_keeps_one_budget_and_indexes_new_host_posts(self):
        dsn, cursor = self.database()
        old, new = 'https://old.example.org/blog/', 'https://new.example.org/journal/'
        bodies = {
            old: (new, b'<html><a href="old-post/">An old post</a><a href="/another-user/post/">Other blog</a></html>'),
            'https://new.example.org/sitemap.xml': ('https://new.example.org/sitemap.xml',
                b'<urlset><url><loc>https://new.example.org/journal/second-post/</loc></url><url><loc>https://new.example.org/another-user/post/</loc></url></urlset>'),
            new + 'old-post/': (new + 'old-post/', b'<html><head><title>Old</title></head><body><article><p>A remembered walk beside the river on a quiet autumn morning.</p></article></body></html>'),
            new + 'second-post/': (new + 'second-post/', b'<html><head><title>Second</title></head><body><article><p>We returned to the forest and watched the first snow fall across the valley.</p></article></body></html>'),
        }
        fetcher = Mock()
        fetcher.fetch.side_effect = lambda url, **kwargs: FetchResult(bodies[url][0], bodies[url][1], 200, content_type='text/html')
        with Store(dsn) as store:
            store.migrate()
            store.set_backfill_budget(old, 4)
            store.enqueue([job('archive', old, root=old, priority=20)])
            self.assertEqual(Worker(store, fetcher).run(), 4)
            cursor.execute('SELECT url FROM pages ORDER BY url')
            self.assertEqual(cursor.fetchall(), [(new + 'old-post/',), (new + 'second-post/',)])
            cursor.execute('SELECT root, cap, discovered FROM crawl_sites')
            self.assertEqual(cursor.fetchall(), [(old, 4, 4)])
            self.assertEqual(store.status()['outbox'], 2)

    def test_restart_backfill_retry_and_index_outage(self):
        dsn, cursor = self.database()
        base = "https://example.org/"
        feed = b'<rss version="2.0"><channel><title>Blog</title><link>https://example.org/</link><item><link>https://example.org/?p=2</link></item></channel></rss>'
        archive = b'<html><a href="/chronicle/">Browse the years</a></html>'
        chronicle = b'<html><nav><a href="/?p=1">Old post</a></nav></html>'
        sitemap = b'<urlset/>'
        new = b'<html><head><title>New post</title></head><body><article><p>A new post about walking by the river. I watched the birds gather on the bridge and enjoyed the quiet of the early morning.</p></article></body></html>'
        old = b'<html><head><title>Old post</title></head><body><article><p>This old post remembers a long journey through the mountains. We set off together before sunrise and returned after the stars appeared in the clear sky.</p></article></body></html>'
        routes = {base + "feed/": feed, base: archive, base + "sitemap.xml": sitemap, base + "chronicle/": chronicle,
                  base + "?p=2": new, base + "?p=1": old}
        failed_once = set()

        def fetch(url, **kwargs):
            if url == base + "?p=1" and url not in failed_once:
                failed_once.add(url)
                raise FetchError("HTTP status 503")
            return FetchResult(url, routes[url], 200, etag='"version1"', content_type="text/html")

        fetcher = Mock()
        fetcher.fetch.side_effect = fetch
        fetcher.robots_sitemaps.return_value = []
        with Store(dsn) as store:
            store.migrate()
            store.enqueue(source_jobs(base + "feed/"))
            self.assertEqual(Worker(store, fetcher).run_batch(), 1)
        # New connection pool/process state: only SQL persists the discovered work.
        with Store(dsn) as store:
            worker = Worker(store, fetcher)
            for _ in range(10):
                if not worker.run_batch():
                    break
            cursor.execute("SELECT status, attempts FROM crawl_jobs WHERE url = %s AND kind = 'page'", (base + "?p=1",))
            self.assertEqual(cursor.fetchone(), ("pending", 1))
            cursor.execute("UPDATE crawl_jobs SET due_at = NOW() WHERE url = %s AND kind = 'page'", (base + "?p=1",))
            self.assertEqual(worker.run_batch(), 1)
            cursor.execute("SELECT url, text FROM pages ORDER BY url")
            rows = cursor.fetchall()
            # Traverse navigation pages without saving their link labels as posts.
            self.assertEqual([row[0] for row in rows], [base + "?p=1", base + "?p=2"])
            self.assertIn("mountains", rows[0][1])
            self.assertEqual(store.status()["outbox"], 2)
            with patch("scraper.worker.streaming_bulk", side_effect=TimeoutError):
                flush_index(store, Mock())
            self.assertEqual(store.status()["outbox"], 2)
            self.assertEqual(store.status()["outbox_failed"], 2)
            store.retry_failed()
            pending = store.index_batch()
            outcomes = [(True, {"index": {"_id": str(row["page_id"]), "status": 200}}) for row in pending]
            with patch("scraper.worker.streaming_bulk", return_value=iter(outcomes)):
                flush_index(store, Mock())
            self.assertEqual(store.status()["outbox"], 0)
            cursor.execute("SELECT COUNT(*) FROM pages")
            self.assertEqual(cursor.fetchone()[0], 2)

    def test_old_post_only_in_paginated_feed_reaches_index_outbox(self):
        dsn, cursor = self.database()
        root = 'https://example.org/blog/'
        feed_url = 'https://example.org/feed'
        latest = b'''<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
            <title>Blog</title><link>https://example.org/blog/</link>
            <atom:link rel="next" href="?page=2"/></channel></rss>'''
        history = b'''<rss version="2.0"><channel><title>Blog</title><link>https://example.org/blog/</link>
            <item><link>https://example.org/blog/2004/walking/</link></item>
            <item><link>https://example.org/blog/contact/</link></item></channel></rss>'''
        post_url = root + '2004/walking/'
        post = b'<html><head><title>A winter walk</title></head><body><article><p>We walked beside the frozen river this morning. The sun rose behind the old bridge and the quiet streets slowly filled with people heading into town.</p></article></body></html>'
        routes = {feed_url: latest, feed_url + '?page=2': history, root: b'<html/>',
                  'https://example.org/sitemap.xml': b'<urlset/>', post_url: post}
        fetcher = Mock()
        fetcher.robots_sitemaps.return_value = []
        fetcher.fetch.side_effect = lambda url, **kwargs: FetchResult(url, routes[url], 200, content_type='text/html')
        with Store(dsn) as store:
            store.migrate()
            store.enqueue(source_jobs(feed_url))
            self.assertEqual(Worker(store, fetcher, workers=2).run(), 5)
            cursor.execute('SELECT url FROM pages')
            self.assertEqual(cursor.fetchall(), [(post_url,)])
            self.assertEqual(store.status()['outbox'], 1)
            self.assertEqual(store.status(root=root)['site']['visited'], 4)
            self.assertEqual(store.status()['due'], 0)
            self.assertNotIn(root + 'contact/', [call.args[0] for call in fetcher.fetch.call_args_list])

    def test_download_outcomes_do_not_store_error_pages_or_repeat_dead_posts(self):
        dsn, cursor = self.database()
        bodies = {
            'article': b'<html><head><title>Journal</title></head><body><article><p>A quiet afternoon beside the river.</p></article></body></html>',
            'noindex': b'<html><head><meta name="robots" content="noindex"></head><body><p>Do not index this.</p></body></html>',
            'missing': b'<html><head><title>Page not found</title></head><body><h1>404: Page not found</h1><p>Try searching.</p></body></html>',
            'challenge': b'<html><head><title>Just a moment...</title></head><body><p>Verify you are human.</p></body></html>',
        }
        tasks = [{'kind': 'page', 'url': f'https://example.org/{name}', 'priority': 90}
                 for name in [*bodies, 'gone']]
        def fetch(url, **kwargs):
            name = url.rsplit('/', 1)[1]
            if name == 'gone':
                raise FetchError('HTTP 410', retryable=False, status_code=410)
            return FetchResult(url, bodies[name], 200, content_type='text/html')
        fetcher = Mock()
        fetcher.fetch.side_effect = fetch
        with Store(dsn) as store:
            store.migrate()
            store.enqueue(tasks)
            self.assertEqual(Worker(store, fetcher).run(), 5)
            cursor.execute('SELECT url, text FROM pages')
            self.assertEqual(cursor.fetchall(), [('https://example.org/article', 'A quiet afternoon beside the river.')])
            cursor.execute('SELECT url, status, error FROM crawl_jobs')
            outcomes = {url.rsplit('/', 1)[1]: (status, error) for url, status, error in cursor.fetchall()}
            self.assertEqual(outcomes['noindex'], ('skipped', 'noindex'))
            self.assertEqual(outcomes['missing'], ('skipped', 'soft_404'))
            self.assertEqual(outcomes['challenge'][0], 'pending')
            self.assertEqual(outcomes['gone'][0], 'failed')
            self.assertEqual(store.status()['outbox'], 1)
            store.enqueue(tasks)  # A repeated feed does not reset these outcomes.
        with Store(dsn) as restarted:
            self.assertEqual(Worker(restarted, fetcher).run(), 0)
        self.assertEqual(fetcher.fetch.call_count, 5)


if __name__ == "__main__":
    unittest.main()
