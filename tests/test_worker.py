import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scraper.fetching import FetchError
from scraper.worker import Worker, flush_index


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.store, self.fetcher = Mock(), Mock()
        self.worker = Worker(self.store, self.fetcher)
        self.task = {"id": 1, "kind": "page", "url": "https://example.org/post/", "payload": {}}

    def test_download_failure_is_saved_for_retry(self):
        self.fetcher.fetch.side_effect = FetchError("temporary failure")
        self.worker.process(self.task)
        self.store.fail.assert_called_once()
        self.store.complete.assert_not_called()

    def test_not_modified_preserves_saved_content(self):
        self.task["etag"] = '"abc"'
        self.fetcher.fetch.return_value = SimpleNamespace(status=304)
        self.worker.process(self.task)
        self.store.complete.assert_called_once_with(self.task, not_modified=True)
        self.assertEqual(self.fetcher.fetch.call_args.kwargs["etag"], '"abc"')

    def test_rate_limit_without_retry_header_still_cools_the_host(self):
        self.fetcher.fetch.side_effect = FetchError("HTTP status 429", status_code=429)
        self.worker.process(self.task)
        self.assertEqual(self.store.fail.call_args.kwargs["status_code"], 429)

    def test_feed_parse_failure_does_not_complete_job(self):
        self.task["kind"] = "feed"
        self.fetcher.fetch.return_value = SimpleNamespace(status=200, body=b'<html>not a feed</html>', url=self.task["url"])
        self.worker.process(self.task)
        self.store.fail.assert_called_once()
        self.store.complete.assert_not_called()

    def test_page_is_committed_through_store_not_directly_indexed(self):
        self.fetcher.fetch.return_value = SimpleNamespace(status=200, body=b'<html/>', url=self.task["url"], content_type="text/html", etag="new", last_modified=None)
        with patch("scraper.worker.extract_page", return_value={"text": "a post"}):
            self.worker.process(self.task)
        self.assertEqual(self.store.complete.call_args.kwargs["page"], {"text": "a post"})

    def test_bulk_partial_failure_only_acknowledges_success(self):
        rows = [{"page_id": i, "revision": 4, "title": "Post", "url": "https://example.org/", "date": None, "text": "text", "scraped_on_date": None} for i in (1, 2)]
        self.store.index_batch.return_value = rows
        outcomes = [(True, {"index": {"_id": "1", "status": 201}}), (False, {"index": {"_id": "2", "status": 429}})]
        with patch("scraper.worker.streaming_bulk", return_value=iter(outcomes)):
            self.assertEqual(flush_index(self.store, Mock()), 2)
        self.store.finish_index.assert_called_once_with([(1, 4)], [(2, 4, "Elasticsearch HTTP 429")])

    def test_bulk_transport_failure_keeps_pending_work(self):
        self.store.index_batch.return_value = [{"page_id": 1, "revision": 2, "title": "Post", "url": "https://example.org/", "date": None, "text": "text", "scraped_on_date": None}]
        with patch("scraper.worker.streaming_bulk", side_effect=TimeoutError):
            flush_index(self.store, Mock())
        self.store.finish_index.assert_called_once_with([], [(1, 2, "TimeoutError")])


if __name__ == "__main__":
    unittest.main()
