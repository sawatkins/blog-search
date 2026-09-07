"""Opt-in real Elasticsearch checks using only a uniquely named disposable index."""

import os
import unittest
from unittest.mock import Mock
from uuid import uuid4

from elasticsearch import Elasticsearch

from scraper.worker import flush_index
from scraper.scraper import ensure_index


@unittest.skipUnless(os.environ.get('TEST_ELASTICSEARCH_URL'), 'TEST_ELASTICSEARCH_URL is not set')
class IndexDeliveryTests(unittest.TestCase):
    def test_retry_is_idempotent_and_delayed_old_write_cannot_replace_new_content(self):
        client = Elasticsearch(os.environ['TEST_ELASTICSEARCH_URL'], request_timeout=10, max_retries=0)
        self.addCleanup(client.close)
        index = 'crawler-review-test-' + uuid4().hex
        client.indices.create(index=index, settings={'number_of_shards': 1, 'number_of_replicas': 0})
        # Only this test's exact generated index is ever removed.
        self.addCleanup(lambda: client.indices.delete(index=index))
        row = {'page_id': 1, 'revision': 20, 'title': 'New version', 'url': 'https://example.org/post',
               'date': '2020-01-01', 'text': 'Current content', 'scraped_on_date': None}
        store = Mock()
        store.index_batch.return_value = [row]
        self.assertEqual(flush_index(store, client, index), 1)
        store.finish_index.assert_called_with([(1, 20)], [])
        self.assertEqual(flush_index(store, client, index), 1)
        store.finish_index.assert_called_with([(1, 20)], [])
        store.index_batch.return_value = [{**row, 'revision': 19, 'text': 'Stale content'}]
        self.assertEqual(flush_index(store, client, index), 1)
        # Keep unexplained conflicts visible, including a mismatched restored DB.
        store.finish_index.assert_called_with([], [(1, 19, 'Elasticsearch HTTP 409')])
        saved = client.get(index=index, id='1')
        self.assertEqual(saved['_source']['text'], 'Current content')
        self.assertEqual(saved['_version'], 20)

    def test_deletion_is_retryable_idempotent_and_rejects_older_write(self):
        client = Elasticsearch(os.environ['TEST_ELASTICSEARCH_URL'], request_timeout=10, max_retries=0)
        self.addCleanup(client.close)
        index = 'crawler-review-test-' + uuid4().hex
        ensure_index(client, index)
        self.addCleanup(lambda: client.indices.delete(index=index))
        self.assertEqual(client.indices.get_settings(index=index)[index]['settings']['index']['gc_deletes'], '1h')
        row = {'page_id': 1, 'revision': 20, 'title': 'Post', 'url': 'https://example.org/post',
               'date': None, 'text': 'Content', 'scraped_on_date': None}
        store = Mock()
        store.index_batch.return_value = [row]
        flush_index(store, client, index)
        store.index_batch.return_value = [{'page_id': 1, 'revision': 21, 'operation': 'delete'}]
        flush_index(store, client, index)
        store.finish_index.assert_called_with([(1, 21)], [])
        self.assertFalse(client.exists(index=index, id='1'))
        flush_index(store, client, index)
        store.finish_index.assert_called_with([(1, 21)], [])
        store.index_batch.return_value = [row]
        flush_index(store, client, index)
        store.finish_index.assert_called_with([], [(1, 20, 'Elasticsearch HTTP 409')])
        self.assertFalse(client.exists(index=index, id='1'))


if __name__ == '__main__':
    unittest.main()
