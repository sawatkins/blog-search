import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from scraper.http_cache import HttpCache


class HttpCacheTests(unittest.TestCase):
    def test_cache_expiry_and_old_request_history_are_cleaned_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite3'
            with patch('time.time', return_value=1000):
                cache = HttpCache(path)
                cache.save_robots('https://example.org', 'User-agent: *\nDisallow: /', 3600)
                cache.requested('example.org')
                self.assertIsNotNone(cache.robots('https://example.org'))
                self.assertIsNone(cache.robots('http://example.org'))
                cache.close()
            with patch('time.time', return_value=90000):
                cache = HttpCache(path)
                self.assertIsNone(cache.robots('https://example.org'))
                self.assertEqual(cache.recent_requests(), {})
                self.assertEqual(cache._connection.execute('SELECT COUNT(*) FROM robots').fetchone()[0], 0)
                cache.close()

    def test_eight_threads_share_atomic_cache_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = HttpCache(Path(directory) / 'cache.sqlite3')
            try:
                def write(number):
                    cache.save_robots(f'https://{number}.example.org', 'User-agent: *', 3600)
                    cache.requested(f'{number}.example.org')
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(write, range(24)))
                self.assertEqual(len(cache.recent_requests()), 24)
                self.assertTrue(all(cache.robots(f'https://{number}.example.org') for number in range(24)))
            finally:
                cache.close()
