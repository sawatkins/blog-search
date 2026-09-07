import unittest
from unittest.mock import patch

from scraper.records import clean_title, content_fingerprint, nonarticle_reason, url_variant_key


class RecordTests(unittest.TestCase):
    def test_leading_emoji_clusters_are_removed(self):
        for title in ('🚀 Hello', '👩🏽‍💻 Hello', '🇬🇧 Hello', '1️⃣ Hello', '#️⃣ Hello',
                      '👨‍👩‍👧‍👦 Hello', '☀️ Hello', '❤ Hello', '  🔥 🚀 Hello', '\ufeff📝 Hello', '\ufeff 📝 Hello'):
            with self.subTest(title=title):
                self.assertEqual(clean_title(title, 'https://example.org/post'), 'Hello')

    def test_text_numbers_accents_and_interior_emoji_are_preserved(self):
        for title in ('2026 in review', '1. Introduction', '# Python', '* Notes', 'Café 日本語',
                      '© 2026 Author', '™ trademark', '∑ maths', 'Hello 🚀', '☀︎ sun', '-1 degrees'):
            with self.subTest(title=title):
                self.assertEqual(clean_title(title, 'https://example.org/post'), title)

    def test_missing_and_all_emoji_titles_have_nonempty_fallback(self):
        for title in (None, '', ' ', '🚀 🔥'):
            self.assertEqual(clean_title(title, 'https://example.org/post'), 'example.org')

    def test_title_cleaning_is_idempotent(self):
        first = clean_title(' \ufeff🔥 A\x00  title\nwith spaces ', 'https://example.org/')
        self.assertEqual(first, 'A title with spaces')
        self.assertEqual(clean_title(first, 'https://example.org/'), first)

    def test_content_hash_ignores_whitespace_but_preserves_meaningful_changes(self):
        self.assertEqual(content_fingerprint('One\n two\u00a0three'), content_fingerprint('One two three'))
        self.assertNotEqual(content_fingerprint('One two'), content_fingerprint('One too'))

    def test_bot_challenge_is_not_an_article_but_discussion_is(self):
        self.assertEqual(nonarticle_reason("Making sure you're not a bot! Loading... This website uses Anubis version 2."),
                         'browser_challenge')
        self.assertIsNone(nonarticle_reason('I wrote about Anubis and cookie notices today.'))

    def test_known_boilerplate_only_matches_the_entire_body(self):
        body = 'Share this post with your friends.'
        with patch.dict('scraper.records.NON_ARTICLE_HASHES', {content_fingerprint(body): 'site_furniture'}):
            self.assertEqual(nonarticle_reason('Share this post\nwith your friends.'), 'site_furniture')
            self.assertIsNone(nonarticle_reason('An actual article precedes this. ' + body))

    def test_distinct_fragment_posts_are_not_url_variants(self):
        self.assertNotEqual(url_variant_key('https://blog.example.org/year.html#first'),
                            url_variant_key('https://blog.example.org/year.html#second'))
