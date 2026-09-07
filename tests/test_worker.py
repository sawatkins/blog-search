import unittest
import time
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scraper.fetching import FetchError, FetchResult
from scraper.worker import CrawlPaused, Worker, flush_index


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
        self.task['resolved_url'] = self.task['url']
        self.fetcher.fetch.return_value = SimpleNamespace(status=304)
        self.worker.process(self.task)
        self.store.complete.assert_called_once_with(self.task, not_modified=True)
        self.assertEqual(self.fetcher.fetch.call_args.kwargs["etag"], '"abc"')

    def test_legacy_validators_without_a_known_destination_are_not_sent(self):
        self.task.update(etag='"unknown-origin"', last_modified='old date', resolved_url=None)
        self.fetcher.fetch.side_effect = FetchError('stop after inspecting request')
        self.worker.process(self.task)
        self.assertIsNone(self.fetcher.fetch.call_args.kwargs['etag'])
        self.assertIsNone(self.fetcher.fetch.call_args.kwargs['last_modified'])

    def test_rate_limit_without_retry_header_still_cools_the_host(self):
        self.fetcher.fetch.side_effect = FetchError("HTTP status 429", status_code=429)
        self.worker.process(self.task)
        self.assertEqual(self.store.fail.call_args.kwargs["status_code"], 429)

    def test_failure_category_reaches_the_durable_retry_policy(self):
        for failure_kind in ('robots_denied', 'tls_error'):
            self.fetcher.fetch.side_effect = FetchError('deferred', failure_kind=failure_kind)
            self.worker.process(self.task)
            self.assertEqual(self.store.fail.call_args.kwargs['failure_kind'], failure_kind)

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

    def test_queued_utility_page_is_skipped_without_download(self):
        self.task.update(url="https://example.org/contact/", payload={"root": "https://example.org/"})
        self.worker.process(self.task)
        self.fetcher.fetch.assert_not_called()
        self.store.complete.assert_called_once_with(self.task, skip_reason="utility_page")

    def test_paginated_feed_is_parsed_as_history_without_reseeding_homepage(self):
        self.task.update(kind="feed_page", payload={"root": "https://example.org/", "depth": 2})
        self.fetcher.fetch.return_value = SimpleNamespace(
            status=200, body=b'<feed/>', url=self.task["url"], etag=None, last_modified=None)
        with patch("scraper.worker.feed_jobs", return_value=([], "https://example.org/")) as parse:
            self.worker.process(self.task)
        parse.assert_called_once_with(b'<feed/>', self.task["url"], root="https://example.org/", depth=2, historical=True)
        self.fetcher.robots_sitemaps.assert_not_called()

    def test_homepage_redirect_moves_scope_without_resetting_history_budget(self):
        old, new = 'https://old.example.org/blog/', 'https://new.example.org/journal/'
        self.task.update(kind='archive', url=old, payload={'root': old})
        self.fetcher.fetch.return_value = SimpleNamespace(
            status=200, url=new, body=b'<html><a href="old-post/">Older post</a><a href="/other-user/post/">Other user</a></html>',
            etag=None, last_modified=None)
        self.worker.process(self.task)
        links = self.store.complete.call_args.kwargs['links']
        self.assertEqual({link['url'] for link in links}, {'https://new.example.org/sitemap.xml', new + 'old-post/'})
        for link in links:
            self.assertEqual(link['payload']['root'], old)
            self.assertEqual(link['payload']['scope_root'], new)

    def test_moved_blog_descendant_discovery_keeps_the_original_budget_root(self):
        old, new = 'https://old.example.org/', 'https://new.example.org/journal/'
        self.task.update(kind='archive', url=new + 'archive/', payload={'root': old, 'scope_root': new})
        self.fetcher.fetch.return_value = SimpleNamespace(
            status=200, url=self.task['url'], body=b'<html><a href="../old-post/">Old</a><a href="/another-blog/post/">Other</a></html>',
            etag=None, last_modified=None)
        self.worker.process(self.task)
        links = self.store.complete.call_args.kwargs['links']
        self.assertEqual([link['url'] for link in links], [new + 'old-post/'])
        self.assertEqual(links[0]['payload'], {'root': old, 'scope_root': new, 'depth': 1})

    def test_redirected_non_root_archive_does_not_authorize_another_site(self):
        self.task.update(kind='archive', url='https://example.org/archive/', payload={'root': 'https://example.org/'})
        self.fetcher.fetch.return_value = SimpleNamespace(
            status=200, url='https://other.example.org/', body=b'<html><a href="/post/">Unrelated</a></html>',
            etag=None, last_modified=None)
        self.worker.process(self.task)
        self.assertEqual(self.store.complete.call_args.kwargs['links'], [])

    def test_continuous_worker_refills_before_slow_job_finishes(self):
        third_started = Event()
        guard = Lock()
        active = peak = 0
        tasks = iter([{"id": number} for number in range(3)])

        def claim(limit):
            return [task for _ in range(limit) if (task := next(tasks, None)) is not None]

        def process(task):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                if task["id"] == 0:
                    self.assertTrue(third_started.wait(2), "Slow job blocked the free worker slot")
                elif task["id"] == 2:
                    third_started.set()
            finally:
                with guard:
                    active -= 1

        self.store.claim.side_effect = claim
        worker = Worker(self.store, self.fetcher, workers=2)
        worker.process = process
        self.assertEqual(worker.run(), 3)
        self.assertLessEqual(peak, 2)
        self.assertEqual(active, 0)

    def test_continuous_worker_limits_claims_and_honors_stop_and_deadline(self):
        self.store.claim.side_effect = lambda limit: [{"id": number} for number in range(limit)]
        worker = Worker(self.store, self.fetcher, workers=2)
        worker.process = Mock()
        self.assertEqual(worker.run(max_jobs=3), 3)
        self.assertEqual(worker.process.call_count, 3)
        self.store.claim.reset_mock()
        self.assertEqual(worker.run(max_jobs=0), 0)
        self.assertEqual(worker.run(deadline=time.monotonic() - 1), 0)
        stop = Event()
        stop.set()
        self.assertEqual(worker.run(stop=stop), 0)
        self.store.claim.assert_not_called()

    def test_stop_drains_already_claimed_work_and_no_more(self):
        stop = Event()
        self.store.claim.return_value = [{"id": 1}, {"id": 2}]
        worker = Worker(self.store, self.fetcher, workers=2)
        worker.process = Mock(side_effect=lambda task: stop.set())
        self.assertEqual(worker.run(stop=stop), 2)
        self.assertEqual(worker.process.call_count, 2)
        self.store.claim.assert_called_once_with(2)

    def test_worker_propagates_database_failure_without_claiming_more(self):
        self.store.claim.return_value = [{"id": 1}]
        worker = Worker(self.store, self.fetcher, workers=1)
        worker.process = Mock(side_effect=RuntimeError("database unavailable"))
        with self.assertRaises(RuntimeError):
            worker.run()
        self.store.claim.assert_called_once()

    def test_historical_page_follows_unfamiliar_archive_links_with_scope_and_depth(self):
        self.task.update(priority=10, payload={"root": "https://example.org/blog/", "depth": 2})
        self.fetcher.fetch.return_value = SimpleNamespace(
            status=200, url="https://example.org/blog/chronicle/", content_type="text/html",
            body=b'<html><a href="2004/">2004</a><a href="/other-blog/post">Other blog</a></html>',
            etag=None, last_modified=None,
        )
        with patch("scraper.worker.extract_page", return_value=None):
            self.worker.process(self.task)
        saved = self.store.complete.call_args.kwargs
        self.assertEqual("no_extractable_text", saved["skip_reason"])
        self.assertEqual(["https://example.org/blog/chronicle/2004/"], [link["url"] for link in saved["links"]])
        self.assertEqual({"root": "https://example.org/blog/", "depth": 3}, saved["links"][0]["payload"])
        self.assertEqual(10, saved["links"][0]["priority"])

        self.task["payload"]["depth"] = 5
        with patch("scraper.worker.extract_page", return_value=None):
            self.worker.process(self.task)
        self.assertEqual([], self.store.complete.call_args.kwargs["links"])

    def test_recent_feed_posts_and_plain_text_do_not_expand_historical_discovery(self):
        for priority, content_type in ((90, "text/html"), (10, "text/plain")):
            with self.subTest(priority=priority, content_type=content_type):
                self.task["priority"] = priority
                self.fetcher.fetch.return_value = SimpleNamespace(
                    status=200, url=self.task["url"], content_type=content_type,
                    body=b'<html><a href="/old-post">Old post</a></html>', etag=None, last_modified=None,
                )
                with patch("scraper.worker.extract_page", return_value={"text": "A post"}):
                    self.worker.process(self.task)
                self.assertEqual([], self.store.complete.call_args.kwargs["links"])

    def test_legacy_page_job_for_archive_listing_is_followed_but_not_indexed(self):
        self.task.update(priority=10, payload={"root": "https://example.org/", "depth": 0})
        self.fetcher.fetch.return_value = SimpleNamespace(
            status=200, url="https://example.org/post/", content_type="text/html",
            body=b'<html><a href="/2004/first/">First post</a></html>', etag=None, last_modified=None,
        )
        with patch("scraper.worker.extract_page") as extract:
            self.worker.process(self.task)
        extract.assert_not_called()
        saved = self.store.complete.call_args.kwargs
        self.assertIsNone(saved["page"])
        self.assertEqual("archive_listing", saved["skip_reason"])
        self.assertEqual(["https://example.org/2004/first/"], [link["url"] for link in saved["links"]])

    def test_bulk_partial_failure_only_acknowledges_success(self):
        rows = [{"page_id": i, "revision": 4, "title": "Post", "url": "https://example.org/", "date": None, "text": "text", "scraped_on_date": None} for i in (1, 2)]
        self.store.index_batch.return_value = rows
        outcomes = [(True, {"index": {"_id": "1", "status": 201}}), (False, {"index": {"_id": "2", "status": 429}})]
        with patch("scraper.worker.streaming_bulk", return_value=iter(outcomes)) as bulk:
            self.assertEqual(flush_index(self.store, Mock()), 2)
        self.assertTrue(all(action['_version'] == 4 and action['_version_type'] == 'external_gte'
                            for action in bulk.call_args.args[1]))
        self.store.finish_index.assert_called_once_with([(1, 4)], [(2, 4, "Elasticsearch HTTP 429")])

    def test_bulk_transport_failure_keeps_pending_work(self):
        self.store.index_batch.return_value = [{"page_id": 1, "revision": 2, "title": "Post", "url": "https://example.org/", "date": None, "text": "text", "scraped_on_date": None}]
        with patch("scraper.worker.streaming_bulk", side_effect=TimeoutError):
            flush_index(self.store, Mock())
        self.store.finish_index.assert_called_once_with([], [(1, 2, "TimeoutError")])


class WorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [{'id': i, 'kind': 'page', 'url': f'https://blog-{i}.example/post'} for i in range(100)]
        self.store, self.fetcher = Mock(), Mock()

        def claim(limit):
            result = self.tasks[:limit]
            del self.tasks[:limit]
            return result

        self.store.claim.side_effect = claim
        self.worker = Worker(self.store, self.fetcher, workers=1)
        self.fetcher.fetch.side_effect = lambda url, **_: self.network_failure()

    @staticmethod
    def network_failure(host=None):
        raise FetchError('Connection failed', failure_kind='network_error', host=host)

    def test_outage_stops_intake_and_stays_stopped_across_runs_and_batches(self):
        self.assertEqual(self.worker.run(max_jobs=2), 2)
        with self.assertRaisesRegex(CrawlPaused, 'five connection/DNS failures'):
            self.worker.run()
        self.assertEqual(self.fetcher.fetch.call_count, 5)
        self.assertEqual(len(self.tasks), 95)
        self.assertEqual(self.store.fail.call_count, 4)
        self.store.defer.assert_called_once()
        claims = self.store.claim.call_count
        for start in (self.worker.run, self.worker.run_batch):
            with self.assertRaises(CrawlPaused):
                start()
        self.assertEqual(self.store.claim.call_count, claims)

    def test_parallel_outage_is_bounded_and_every_claimed_job_is_accounted_for(self):
        self.worker = Worker(self.store, self.fetcher, workers=8)
        with self.assertRaises(CrawlPaused):
            self.worker.run()
        claimed = 100 - len(self.tasks)
        self.assertLessEqual(claimed, 12)  # Five failures plus at most seven in flight.
        self.assertEqual(claimed, self.store.fail.call_count + self.store.defer.call_count)

    def test_one_failed_redirect_destination_is_not_a_machine_wide_outage(self):
        self.fetcher.fetch.side_effect = lambda url, **_: self.network_failure('shared-cdn.example')
        self.assertEqual(self.worker.run(), 100)
        self.assertEqual(self.store.fail.call_count, 100)
        self.store.defer.assert_not_called()

    def test_http_responses_reset_the_connection_failure_streak(self):
        for healthy in (FetchResult('https://healthy.example/post', b'', 304),
                        FetchError('Not found', retryable=False, status_code=404)):
            with self.subTest(healthy=type(healthy).__name__):
                self.setUp()
                self.fetcher.fetch.side_effect = [
                    healthy if i % 5 == 4 else FetchError('Connection failed', failure_kind='network_error')
                    for i in range(100)
                ]
                self.assertEqual(self.worker.run(), 100)
                self.store.defer.assert_not_called()

    def test_ordinary_failures_do_not_trip_guard(self):
        self.fetcher.fetch.side_effect = [
            FetchError('Not found', retryable=False, status_code=404),
            FetchError('Unavailable', status_code=503),
            FetchError('Slow down', status_code=429),
            FetchError('Robots', failure_kind='robots_denied', retryable=False),
            FetchError('Certificate', failure_kind='tls_error'),
            FetchError('Invalid compressed body'),
        ] * 10
        self.assertEqual(self.worker.run(max_jobs=60), 60)
        self.assertEqual(self.store.fail.call_count, 60)
        self.store.defer.assert_not_called()

    def test_old_scattered_connection_errors_do_not_trip_guard(self):
        with patch('scraper.worker.time.monotonic', side_effect=[0, 100, 200, 300, 400]):
            for task in self.tasks[:5]:
                self.worker.process(task)
        self.assertEqual(self.store.fail.call_count, 5)
        self.store.defer.assert_not_called()

    def test_internal_error_is_not_charged_as_a_bad_url(self):
        self.fetcher.fetch.side_effect = RuntimeError('sensitive details')
        with self.assertRaisesRegex(CrawlPaused, 'unexpected RuntimeError') as caught:
            self.worker.run()
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertEqual(len(self.tasks), 99)
        self.store.fail.assert_not_called()
        self.store.defer.assert_called_once()

    def test_failed_release_still_stops_with_lease_recovery_warning(self):
        self.fetcher.fetch.side_effect = RuntimeError('broken')
        self.store.defer.side_effect = OSError('database unavailable')
        with self.assertLogs('scraper.worker', level='ERROR') as logs, self.assertRaises(CrawlPaused):
            self.worker.run()
        self.assertTrue(any('lease must expire' in message for message in logs.output))
        self.assertEqual(len(self.tasks), 99)

    def test_successful_inflight_job_is_saved_after_intake_stops(self):
        paused = Event()
        self.store.defer.side_effect = lambda *_: paused.set()

        def fetch(url, **kwargs):
            if url == 'https://blog-0.example/post':
                self.assertTrue(paused.wait(3), 'Other slot did not stop on the outage')
                return FetchResult(url, b'An article', 200, content_type='text/plain')
            self.network_failure()

        self.fetcher.fetch.side_effect = fetch
        self.worker = Worker(self.store, self.fetcher, workers=2)
        with patch('scraper.worker.extract_page', return_value={'text': 'An article'}), self.assertRaises(CrawlPaused):
            self.worker.run()
        self.store.complete.assert_called_once()
        self.assertEqual(self.store.complete.call_args.kwargs['page'], {'text': 'An article'})
        self.assertEqual(len(self.tasks), 94)

    def test_malformed_sitemap_is_an_individual_job_failure(self):
        self.tasks[0]['kind'] = 'sitemap'
        self.fetcher.fetch.return_value = FetchResult(self.tasks[0]['url'], b'<urlset><url></urlset>', 200)
        self.fetcher.fetch.side_effect = None
        self.assertEqual(self.worker.run(max_jobs=1), 1)
        self.store.fail.assert_called_once()
        self.store.defer.assert_not_called()

    def test_corrupt_gzipped_sitemap_does_not_stop_the_run(self):
        self.tasks[0]['kind'] = 'sitemap'
        self.fetcher.fetch.return_value = FetchResult(self.tasks[0]['url'], b'\x1f\x8b\x08\x00truncated', 200)
        self.fetcher.fetch.side_effect = None
        self.assertEqual(self.worker.run(max_jobs=1), 1)
        self.store.fail.assert_called_once()
        self.store.defer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
