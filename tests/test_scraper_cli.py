"""CLI orchestration tests: no production services or external network calls."""

import unittest
import signal
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scraper.content import SMALLCOMIC_URL, SMALLWEB_URL
from scraper.scraper import main, parser, run, sync_sources
from scraper.storage import SourceSyncError
from scraper.worker import CrawlPaused


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.store, self.fetcher = Mock(), Mock()
        self.responses = {
            SMALLWEB_URL: b'https://example.org/feed\nhttps://comic.example.org/feed',
            SMALLCOMIC_URL: b'https://comic.example.org/feed',
        }
        self.fetcher.fetch.side_effect = lambda url: SimpleNamespace(body=self.responses[url])

    def test_sync_excludes_comics_before_any_database_write(self):
        sync_sources(self.store, self.fetcher)
        tasks = self.store.reconcile_sources.call_args.args[0]
        self.assertEqual([task['url'] for task in tasks], ['https://example.org/feed'])

    def test_invalid_exclusions_leave_existing_queue_untouched(self):
        self.responses[SMALLCOMIC_URL] = b'<html>Service unavailable</html>'
        with self.assertRaises(ValueError):
            sync_sources(self.store, self.fetcher)
        self.store.reconcile_sources.assert_not_called()

    def test_unavailable_exclusions_leave_existing_queue_untouched(self):
        self.fetcher.fetch.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            sync_sources(self.store, self.fetcher)
        self.store.reconcile_sources.assert_not_called()

    def test_local_lists_do_not_require_network(self):
        with patch('scraper.scraper.Path.read_text', side_effect=[body.decode() for body in self.responses.values()]):
            sync_sources(self.store, self.fetcher, 'feeds.txt', 'comics.txt')
        self.fetcher.fetch.assert_not_called()
        self.assertEqual(len(self.store.reconcile_sources.call_args.args[0]), 1)
        self.assertFalse(self.store.reconcile_sources.call_args.kwargs['replace'])

    def test_all_excluded_does_not_write(self):
        self.responses[SMALLWEB_URL] = self.responses[SMALLCOMIC_URL]
        with self.assertRaises(ValueError):
            sync_sources(self.store, self.fetcher)
        self.store.reconcile_sources.assert_not_called()

    def test_safe_source_error_reaches_the_operator(self):
        with patch('scraper.scraper.load_dotenv'), patch('scraper.scraper.Store'), \
                patch('scraper.scraper.Fetcher'), \
                patch('scraper.scraper.sync_sources', side_effect=SourceSyncError('Inspect the snapshot before --allow-large-removal')):
            with self.assertLogs('scraper.scraper', level='ERROR') as logs:
                self.assertEqual(main(['sync']), 1)
        self.assertIn('--allow-large-removal', logs.output[0])


class RunTests(unittest.TestCase):
    def test_default_delay_is_five_seconds(self):
        self.assertEqual(parser().parse_args(['run']).delay, 5.0)

    def test_initialization_failure_closes_resources_and_restores_signal_handlers(self):
        args = parser().parse_args(['run'])
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch('scraper.scraper.elasticsearch_client') as client, \
                patch('scraper.scraper.Fetcher', side_effect=ValueError('invalid configuration')), \
                patch('scraper.scraper.process_lock', return_value=nullcontext()):
            with self.assertRaises(ValueError):
                run(Mock(), args)
        client.return_value.close.assert_called_once()
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})

    def test_run_uses_continuous_worker_and_preserves_job_limit(self):
        args = parser().parse_args(['run', '--max-jobs', '3', '--workers', '2'])
        with patch('scraper.scraper.elasticsearch_client'), patch('scraper.scraper.Fetcher'), \
                patch('scraper.scraper.Worker') as worker, \
                patch('scraper.scraper.process_lock', return_value=nullcontext()), \
                patch('scraper.scraper.flush_index', return_value=0):
            worker.return_value.run.return_value = 3
            run(Mock(), args)
        worker.return_value.run.assert_called_once()
        self.assertEqual(worker.return_value.run.call_args.kwargs['max_jobs'], 3)
        worker.return_value.run_batch.assert_not_called()

    def test_index_outage_does_not_prevent_crawling(self):
        args = parser().parse_args(['run'])
        with patch('scraper.scraper.elasticsearch_client'), patch('scraper.scraper.Fetcher'), \
                patch('scraper.scraper.Worker') as worker, \
                patch('scraper.scraper.process_lock', return_value=nullcontext()), \
                patch('scraper.scraper.flush_index', side_effect=TimeoutError):
            worker.return_value.run.side_effect = [2, 0]
            run(Mock(), args)
        self.assertEqual(worker.return_value.run.call_count, 2)

    def test_watch_does_not_restart_after_safety_stop(self):
        args = parser().parse_args(['run', '--watch'])
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch('scraper.scraper.elasticsearch_client') as client, patch('scraper.scraper.Fetcher') as fetcher, \
                patch('scraper.scraper.Worker') as worker, \
                patch('scraper.scraper.process_lock', return_value=nullcontext()), \
                patch('scraper.scraper.flush_index', return_value=0):
            worker.return_value.run.side_effect = CrawlPaused('Check connectivity')
            with self.assertRaises(CrawlPaused):
                run(Mock(), args)
        worker.return_value.run.assert_called_once()
        client.return_value.close.assert_called_once()
        fetcher.return_value.close.assert_called_once()
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})

    def test_safety_stop_is_reported_and_returns_nonzero(self):
        with patch('scraper.scraper.load_dotenv'), patch('scraper.scraper.Store'), \
                patch('scraper.scraper.run', side_effect=CrawlPaused('Check connectivity before restarting')):
            with self.assertLogs('scraper.scraper', level='ERROR') as logs:
                self.assertEqual(main(['run']), 1)
        self.assertIn('Check connectivity before restarting', logs.output[0])

    def test_extraction_retry_is_bounded_and_does_not_start_crawler(self):
        for arguments, limit in ((['retry-skipped'], 100), (['retry-skipped', '--limit', '12'], 12)):
            with self.subTest(limit=limit), patch('scraper.scraper.load_dotenv'), \
                    patch('scraper.scraper.Store') as store, patch('scraper.scraper.run') as start:
                self.assertEqual(main(arguments), 0)
                store.return_value.__enter__.return_value.retry_skipped.assert_called_once_with(limit=limit)
                start.assert_not_called()

    def test_extraction_retry_rejects_nonpositive_limit_before_opening_database(self):
        with patch('scraper.scraper.load_dotenv'), patch('scraper.scraper.Store') as store, \
                patch('sys.stderr'), self.assertRaises(SystemExit):
            main(['retry-skipped', '--limit', '0'])
        store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
