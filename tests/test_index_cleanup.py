import unittest

from ops.prepare_index_cleanup import repair_url, same_article


class CleanupRulesTests(unittest.TestCase):
    def test_repair_keeps_fragment_posts_and_meaningful_queries(self):
        self.assertEqual(repair_url('https://blog.example.org/year.html#first'), 'https://blog.example.org/year.html#first')
        self.assertEqual(repair_url('https://blog.example.org/post?id=4&utm_source=rss'), 'https://blog.example.org/post?id=4')
        self.assertEqual(repair_url('https://blog.example.org/Some post.html#section'),
                         'https://blog.example.org/Some%20post.html#section')
        self.assertIsNone(repair_url('http://127.0.0.1/private'))

    def test_distinct_fragments_are_not_merged_by_shared_title_or_text(self):
        a = {'id': 1, 'new_url': 'https://blog.example.org/log#first', 'content_hash': 'same',
             'clean_title': 'Blog archive', 'date': '2025-01-01'}
        b = {**a, 'id': 2, 'new_url': 'https://blog.example.org/log#second'}
        self.assertIsNone(same_article(a, b, {}))

    def test_tracking_variants_are_the_same_resource_after_repair(self):
        a = {'id': 1, 'new_url': repair_url('https://blog.example.org/post?utm_source=rss')}
        b = {'id': 2, 'new_url': repair_url('https://blog.example.org/post')}
        self.assertEqual(same_article(a, b, {}), 'same_normalized_url')
