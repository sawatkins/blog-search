"""Exercise discovery, extraction, retries and indexing together with real SQL."""

import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from scraper.content import source_jobs
from scraper.fetching import FetchError, FetchResult
from scraper.storage import Store
from scraper.worker import Worker, flush_index


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PipelineTests(unittest.TestCase):
    def test_restart_backfill_retry_and_index_outage(self):
        schema = "test_pipeline_" + uuid4().hex
        dsn = make_dsn(os.environ["TEST_DATABASE_URL"], options="-csearch_path=" + schema)
        connection = psycopg2.connect(dsn)
        connection.autocommit = True
        self.addCleanup(connection.close)
        cursor = connection.cursor()
        self.addCleanup(cursor.close)
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        self.addCleanup(lambda: cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))))

        base = "https://example.org/"
        feed = b'<rss version="2.0"><channel><title>Blog</title><link>https://example.org/</link><item><link>https://example.org/?p=2</link></item></channel></rss>'
        archive = b'<html><a href="/?p=1">An old post</a></html>'
        sitemap = b'<urlset><url><loc>https://example.org/?p=1</loc></url></urlset>'
        new = b'<html><head><title>New post</title></head><body><article><p>A new post about walking by the river. I watched the birds gather on the bridge and enjoyed the quiet of the early morning.</p></article></body></html>'
        old = b'<html><head><title>Old post</title></head><body><article><p>This old post remembers a long journey through the mountains. We set off together before sunrise and returned after the stars appeared in the clear sky.</p></article></body></html>'
        routes = {base + "feed/": feed, base: archive, base + "sitemap.xml": sitemap,
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


if __name__ == "__main__":
    unittest.main()
