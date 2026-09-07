import gzip
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from scraper.content import FEED_PAGE_DEPTH, PageRejected, archive_jobs, extract_page, feed_jobs, is_utility_url, sitemap_jobs, source_jobs
from scraper.fetching import FetchError


class ContentTests(unittest.TestCase):
    def test_source_list_is_idempotent_and_preserves_feed_paths(self):
        tasks = source_jobs("https://example.org/feed/ # a blog\nhttps://example.org/feed/\n# comment\nftp://example.org/file")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["url"], "https://example.org/feed/")
        self.assertEqual(tasks[0]["kind"], "feed")

    def test_feed_keeps_all_entries_and_query_based_posts(self):
        entries = "".join(f"<item><title>Post {i}</title><link>https://example.org/?p={i}</link></item>" for i in range(65))
        body = ('<rss version="2.0"><channel><title>Blog</title><link>https://example.org/</link>' + entries + '</channel></rss>').encode()
        tasks, homepage = feed_jobs(body, "https://example.org/feed/")
        self.assertEqual(homepage, "https://example.org/")
        self.assertEqual(len([t for t in tasks if t["kind"] == "page"]), 65)
        self.assertTrue(any(t["kind"] == "archive" for t in tasks))

    def test_feed_without_homepage_still_indexes_posts(self):
        body = b'<rss version="2.0"><channel><title>Blog</title><item><link>https://example.org/post</link></item></channel></rss>'
        tasks, homepage = feed_jobs(body, "https://example.org/alice/feed")
        self.assertIn("https://example.org/post", [t["url"] for t in tasks])
        self.assertIsNone(homepage)
        self.assertTrue(all(t["kind"] == "page" for t in tasks))

    def test_source_exclusions_match_feeds_not_words_or_shared_hosts(self):
        tasks = source_jobs("https://shared.example.org/comic/feed\nhttps://shared.example.org/essays/feed\nhttps://comics-history.example.org/feed",
                            ["https://SHARED.example.org/comic/feed#fragment"])
        self.assertEqual([task["url"] for task in tasks],
                         ["https://comics-history.example.org/feed", "https://shared.example.org/essays/feed"])

    def test_feed_pagination_is_scoped_historical_and_bounded(self):
        body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
            <link href="https://example.org/blog/" rel="alternate"/>
            <link href="?page=2" rel="next"/><link href="https://other.example.org/feed" rel="next"/>
            <entry><title>Post</title><link href="https://example.org/blog/old"/></entry></feed>'''
        tasks, _ = feed_jobs(body, "https://example.org/feed")
        pages = [task for task in tasks if task["kind"] == "feed_page"]
        self.assertEqual([task["url"] for task in pages], ["https://example.org/feed?page=2"])
        self.assertEqual(pages[0]["payload"], {"root": "https://example.org/blog/", "depth": 1})
        tasks, _ = feed_jobs(body, "https://example.org/feed", root="https://example.org/blog/",
                             historical=True, depth=FEED_PAGE_DEPTH)
        self.assertEqual([task["kind"] for task in tasks], ["page"])
        self.assertEqual(tasks[0]["priority"], 10)

    def test_rss_atom_next_link_and_missing_entry_link(self):
        body = b'''<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
            <title>Blog</title><link>https://example.org/</link>
            <atom:link rel="next" href="?page=2"/><item><title>No URL</title></item></channel></rss>'''
        tasks, _ = feed_jobs(body, "https://example.org/feed")
        self.assertEqual(len([task for task in tasks if task["kind"] == "feed_page"]), 1)
        self.assertFalse(any(task["kind"] == "page" for task in tasks))

    def test_json_feed_pagination_and_old_post_urls(self):
        body = json.dumps({'version': 'https://jsonfeed.org/version/1.1', 'title': 'Blog',
                           'home_page_url': 'https://example.org/blog/',
                           'next_url': 'https://example.org/feed.json?page=2',
                           'items': [{'id': 'old', 'url': 'https://example.org/blog/old', 'content_text': 'A short post'}]}).encode()
        tasks, root = feed_jobs(body, 'https://example.org/feed.json')
        self.assertEqual(root, 'https://example.org/blog/')
        self.assertIn(('feed_page', 'https://example.org/feed.json?page=2'), [(item['kind'], item['url']) for item in tasks])
        self.assertIn(('page', 'https://example.org/blog/old'), [(item['kind'], item['url']) for item in tasks])

    def test_utility_filter_preserves_actual_post_slugs_and_short_posts(self):
        root = "https://example.org/blog/"
        for path in ("contact/", "privacy-policy.html", "?s=birds", "wp-login.php", "photo.jpg"):
            self.assertTrue(is_utility_url(root + path, root), path)
        for path in ("2004/about/", "about-the-river/", "?p=42", "comics-and-reading/"):
            self.assertFalse(is_utility_url(root + path, root), path)

    def test_all_discovery_paths_filter_utility_pages(self):
        root = "https://example.org/blog/"
        body = b'<html><a href="contact/">Contact</a><a href="2004/about/">A post</a></html>'
        self.assertEqual([task["url"] for task in archive_jobs(body, root, root)], [root + "2004/about/"])
        xml = b'<urlset><url><loc>https://example.org/blog/contact/</loc></url></urlset>'
        self.assertEqual(sitemap_jobs(xml, root + "sitemap.xml", root), [])
        feed = b'<rss version="2.0"><channel><title>Blog</title><link>https://example.org/blog/</link><item><link>https://example.org/blog/contact/</link></item></channel></rss>'
        self.assertFalse(any(task["kind"] == "page" for task in feed_jobs(feed, root + "feed")[0]))

    def test_sitemap_discovers_old_posts_with_scope(self):
        body = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.org/blog/2004/first/</loc></url><url><loc>https://elsewhere.org/post</loc></url><url><loc>https://example.org/another-user/post</loc></url></urlset>'
        tasks = sitemap_jobs(body, "https://example.org/sitemap.xml", "https://example.org/blog/")
        self.assertEqual([t["url"] for t in tasks], ["https://example.org/blog/2004/first/"])
        self.assertEqual(tasks, sitemap_jobs(gzip.compress(body), "https://example.org/sitemap.xml.gz", "https://example.org/blog/"))

    def test_sitemap_index_and_depth_limit(self):
        body = b'<sitemapindex><sitemap><loc>https://example.org/posts.xml</loc></sitemap></sitemapindex>'
        tasks = sitemap_jobs(body, "https://example.org/sitemap.xml", "https://example.org/")
        self.assertEqual(tasks[0]["kind"], "sitemap")
        self.assertEqual(sitemap_jobs(body, "https://example.org/sitemap.xml", "https://example.org/", depth=8), [])

    def test_sitemap_listings_are_traversed_not_indexed_as_posts(self):
        paths = ["/blog/", "/blog/post/", "/blog/categories/", "/blog/tags/walking/", "/blog/2004/first/"]
        body = ("<urlset>" + "".join(f"<url><loc>https://example.org{path}</loc></url>" for path in paths) + "</urlset>").encode()
        tasks = sitemap_jobs(body, "https://example.org/sitemap.xml", "https://example.org/blog/")
        self.assertEqual(["archive"] * 4 + ["page"], [task["kind"] for task in tasks])
        self.assertEqual([20] * 4 + [10], [task["priority"] for task in tasks])

    def test_query_based_post_is_not_mistaken_for_homepage(self):
        tasks = sitemap_jobs(b'<urlset><url><loc>https://example.org/?p=123</loc></url></urlset>',
                             "https://example.org/sitemap.xml", "https://example.org/")
        self.assertEqual("page", tasks[0]["kind"])

    def test_blog_landing_page_is_discovery_not_an_article(self):
        body = b'<urlset><url><loc>https://example.org/blog/</loc></url><url><loc>https://example.org/blog/a-walk/</loc></url></urlset>'
        tasks = sitemap_jobs(body, 'https://example.org/sitemap.xml', 'https://example.org/')
        self.assertEqual([t['kind'] for t in tasks], ['archive', 'page'])

    def test_empty_archive_does_not_create_a_retry_loop(self):
        for body in (b'', b'   \n'):
            self.assertEqual(archive_jobs(body, 'https://example.org/blog/', 'https://example.org/'), [])

    def test_month_archive_is_discovery_but_dated_post_is_retained(self):
        body = b'<urlset><url><loc>https://example.org/blog/2026/08/</loc></url><url><loc>https://example.org/blog/2026/08/12/a-walk/</loc></url></urlset>'
        tasks = sitemap_jobs(body, 'https://example.org/sitemap.xml', 'https://example.org/blog/')
        self.assertEqual([t['kind'] for t in tasks], ['archive', 'page'])

    def test_sitemap_depth_does_not_consume_html_traversal_depth(self):
        tasks = sitemap_jobs(b'<urlset><url><loc>https://example.org/chronicle/</loc></url></urlset>',
                             "https://example.org/posts.xml", "https://example.org/", depth=7)
        self.assertEqual(0, tasks[0]["payload"]["depth"])

    def test_sitemap_dtd_rejected(self):
        with self.assertRaises(ValueError):
            sitemap_jobs(b'<!DOCTYPE urlset [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset><url><loc>&x;</loc></url></urlset>', "https://example.org/sitemap.xml", "https://example.org/")

    def test_archive_follows_pagination_and_retains_scope(self):
        body = b'<html><a href="?page=2" rel="next">Next</a><a href="2002/post/">Old post</a><a href="https://external.org/post">Outside</a><a href="/other/post">Other user</a><a href="picture.jpg">Photo</a></html>'
        tasks = archive_jobs(body, "https://example.org/blog/", "https://example.org/blog/")
        self.assertEqual({(t["kind"], t["url"]) for t in tasks}, {
            ("archive", "https://example.org/blog/?page=2"),
            ("page", "https://example.org/blog/2002/post/"),
        })
        self.assertEqual(archive_jobs(body, "https://example.org/blog/", "https://example.org/blog/", depth=5), [])

    def test_head_pagination_is_followed_once_and_canonical_is_ignored(self):
        body = b'''<html><head><link rel="next" href="page-two"/>
            <link rel="canonical" href="/canonical"/></head><body>
            <a href="page-two">2</a></body></html>'''
        tasks = archive_jobs(body, "https://example.org/blog/", "https://example.org/blog/")
        self.assertEqual([(task["kind"], task["url"]) for task in tasks],
                         [("archive", "https://example.org/blog/page-two")])

    def test_next_article_is_not_misclassified_as_archive(self):
        body = b'<html><head><link rel="next" href="/2004/second"/></head><body><a href="/2004/second" rel="next">Next</a></body></html>'
        tasks = archive_jobs(body, "https://example.org/2004/first", "https://example.org/")
        self.assertEqual(tasks[0]["kind"], "page")

    def test_short_post_is_not_rejected_for_having_under_100_words(self):
        body = b'<html><head><title>A walk</title></head><body><article><h1>A walk</h1><p>Today I walked along the river and watched the birds gathering on the old bridge. A small, peaceful moment I wanted to remember.</p></article></body></html>'
        page = extract_page(body, "https://example.org/?p=1")
        self.assertIsNotNone(page)
        self.assertIn("river", page["text"])
        self.assertLess(len(page["text"].split()), 100)
        self.assertEqual(page["url"], "https://example.org/?p=1")

    def test_disclosure_setup_code_does_not_replace_the_rest_of_an_article(self):
        body = b'''<html><head><title>A data experiment</title></head><body><main>
            <details><summary>Setup code</summary><pre>import numbers\nload_data()</pre></details>
            <p>We measured the river every day for a year to understand the changes in water level.</p>
            <section><h2>Results</h2><p>The autumn rains filled the valley, but the old bridge remained dry.</p></section>
            <details><summary>More code</summary><pre>print(results)</pre></details>
            <p>Our conclusion is that regular measurements helped the town prepare for winter.</p>
            </main></body></html>'''
        page = extract_page(body, 'https://example.org/experiment')
        for expected in ('load_data()', 'autumn rains', 'print(results)', 'town prepare for winter'):
            self.assertIn(expected, page['text'])

    def test_explicit_article_body_keeps_div_paragraphs_and_metadata(self):
        body = b'''<html><head><title>The river journal</title>
            <meta property="article:published_time" content="2020-05-06T12:00:00Z"></head><body>
            <aside>Unrelated sidebar advertisement</aside><div itemprop="articleBody">
            <p>We set out along the river this morning with a map and a notebook.</p>
            <div>The bridge was closed so we followed the southern path through the village.</div>
            <div>By evening we reached the old mill and watched the birds return to their nests.</div>
            <p><a href="https://example.org/map">The detailed river map and bridge survey explain the whole route.</a></p>
            <section id="comments"><p>Buy unwanted things from a spammer.</p></section>
            </div></body></html>'''
        page = extract_page(body, 'https://example.org/journal')
        self.assertEqual(page['title'], 'The river journal')
        self.assertEqual(page['date'], '2020-05-06')
        self.assertIn('southern path', page['text'])
        self.assertIn('return to their nests', page['text'])
        self.assertIn('bridge survey explain the whole route', page['text'])
        self.assertNotIn('advertisement', page['text'])
        self.assertNotIn('spammer', page['text'])

    def test_tumblr_reactions_are_not_article_text_but_personal_notes_remain(self):
        body = b'''<html><head><title>A quiet moment</title></head><body>
            <article><p>There is a little bird in the garden.</p><ol class="notes"><li>A personal note about the bird.</li></ol></article>
            <section id="post-notes"><h2>100 notes</h2><ol class="notes"><li class="note like">Spammer liked this.</li></ol></section>
            <ol class="notes"><li class="note reblog">Someone reblogged this.</li></ol></body></html>'''
        page = extract_page(body, 'https://example.org/post/123')
        self.assertIn('little bird', page['text'])
        self.assertIn('personal note', page['text'])
        self.assertNotIn('liked this', page['text'])
        self.assertNotIn('reblogged', page['text'])

    def test_article_is_not_duplicated_and_comments_are_removed(self):
        body = b'''<html><head><title>A walk</title>
            <meta property="article:published_time" content="2020-02-03T10:00:00Z"></head>
            <body><nav>Home About Subscribe</nav><article><h1>A walk</h1>
            <p>Today I walked along the river and watched the birds gathering on the old bridge.
            A small, peaceful moment I wanted to remember.</p>
            <pre>def greet():\n    print("hello")\n    return True</pre></article>
            <section id="comments"><p>Buy my unwanted product now and get a huge discount today.</p></section>
            <footer>Copyright 2026</footer></body></html>'''
        page = extract_page(body, 'https://example.org/walk')
        self.assertEqual(page['text'].count('Today I walked'), 1)
        self.assertEqual(page['text'].count('def greet()'), 1)
        self.assertIn('\n    print("hello")\n    return True', page['text'])
        self.assertNotIn('unwanted product', page['text'])
        self.assertNotIn('Copyright', page['text'])
        self.assertNotIn('Subscribe', page['text'])
        self.assertEqual(page['date'], '2020-02-03')

    def test_navigation_only_is_not_an_article(self):
        body = b'<html><head><title>Archive</title></head><body><nav><a href="/a">A walk</a><a href="/b">Mountain trip</a></nav></body></html>'
        self.assertIsNone(extract_page(body, 'https://example.org/chronicle'))
        self.assertIsNone(extract_page(b'<nav><a href="/a">A walk</a><a href="/b">Mountain trip</a></nav>', 'https://example.org/chronicle'))

    def test_obvious_http_200_error_pages_are_not_articles(self):
        body = b'<html><head><title>Page not found</title></head><body><h1>404: Page not found</h1><p>Sorry, try searching or return to the homepage.</p></body></html>'
        with self.assertRaisesRegex(PageRejected, 'soft_404'):
            extract_page(body, 'https://example.org/deleted')

    def test_browser_challenge_is_retryable_not_searchable(self):
        body = b'<html><head><title>Just a moment...</title></head><body><h1>Checking your browser before accessing the website.</h1><p>Enable JavaScript and cookies to continue. Please verify you are human.</p></body></html>'
        with self.assertRaises(FetchError) as error:
            extract_page(body, 'https://example.org/post')
        self.assertTrue(error.exception.retryable)

    def test_post_about_errors_or_comments_is_retained(self):
        body = b'<html><head><title>Building a better 404 page</title></head><body><article id="comments-on-life"><p>Page not found is a familiar error. Today I improved my error pages and wrote some comments about the process.</p></article></body></html>'
        self.assertIn('Today I improved', extract_page(body, 'https://example.org/404-design')['text'])

    def test_meta_and_header_noindex_directives_are_scoped_to_our_bot(self):
        body = '<html><head><title>Journal</title>{}</head><body><article><p>A quiet afternoon beside the river.</p></article></body></html>'
        for name in ('robots', 'BlogSearchBot'):
            with self.subTest(name=name), self.assertRaisesRegex(PageRejected, 'noindex'):
                extract_page(body.format(f'<meta name="{name}" content="noindex, follow">').encode(), 'https://example.org/post')
        for value in ('noindex', 'none', 'BlogSearchBot: noindex', 'otherbot: index, blogsearchbot: noindex'):
            with self.subTest(header=value), self.assertRaisesRegex(PageRejected, 'noindex'):
                extract_page(body.format('').encode(), 'https://example.org/post', robots_header=value)
        page = extract_page(body.format('<meta name="otherbot" content="noindex">').encode(), 'https://example.org/post', robots_header='otherbot: noindex')
        self.assertIsNotNone(page)

    def test_plain_text_has_no_html_interpretation_and_cleans_database_controls(self):
        page = extract_page(b'Hello\x00\x01.\n    2 < 3\nA quiet afternoon.', 'https://example.org/journal.txt', content_type='text/plain; charset=utf-8')
        self.assertEqual(page['text'], 'Hello.\n    2 < 3\nA quiet afternoon.')
        self.assertEqual(page['title'], 'example.org')
        self.assertIsNone(page['date'])
        self.assertIsNone(extract_page(b'\x00\x01 ', 'https://example.org/empty', content_type='text/plain'))

    def test_short_unicode_code_and_repetition_are_retained(self):
        for text in ('Hello.', '雨の音。静かな朝でした。', 'Un café après la pluie.'):
            with self.subTest(text=text):
                body = f'<html><head><title>Journal</title></head><body><article><p>{text}</p></article></body></html>'.encode()
                self.assertIn(text, extract_page(body, 'https://example.org/post')['text'])
        body = b'<html><head><title>A poem</title></head><body><article><p>echo</p><p>echo</p></article></body></html>'
        self.assertEqual(extract_page(body, 'https://example.org/poem')['text'], 'echo\necho')

    def test_declared_legacy_charset_is_decoded(self):
        body = '<html><head><title>Caf\xe9</title></head><body><article><p>Un caf\xe9 apr\xe8s la pluie.</p></article></body></html>'.encode('iso-8859-1')
        page = extract_page(body, 'https://example.org/cafe', content_type='text/html; charset=iso-8859-1')
        self.assertEqual(page['title'], 'Caf\xe9')
        self.assertIn('caf\xe9 apr\xe8s', page['text'])

    def test_extraction_is_local_and_repeatable_across_workers(self):
        body = b'<html><head><title>Journal</title></head><body><article><p>A quiet afternoon beside the river.</p></article></body></html>'
        with patch('socket.getaddrinfo', side_effect=AssertionError('Extraction attempted network I/O')):
            with ThreadPoolExecutor(max_workers=8) as executor:
                pages = list(executor.map(lambda _: extract_page(body, 'https://example.org/post'), range(24)))
        self.assertTrue(all(page == pages[0] for page in pages))

    def test_table_content_survives_extraction(self):
        body = b'<html><head><title>Bird count</title></head><body><article><table><tr><th>Name</th><th>Count</th></tr><tr><td>Birds</td><td>12</td></tr></table></article></body></html>'
        page = extract_page(body, 'https://example.org/count')
        self.assertIn('Birds', page['text'])
        self.assertIn('12', page['text'])

    def test_articles_under_posts_are_indexed_not_only_traversed(self):
        tasks = archive_jobs(b'<html><a href="/posts/">All posts</a><a href="/posts/2004/old-post/">Old post</a></html>',
                             "https://example.org/", "https://example.org/")
        self.assertEqual({(t["kind"], t["url"]) for t in tasks}, {
            ("archive", "https://example.org/posts/"),
            ("page", "https://example.org/posts/2004/old-post/"),
        })


if __name__ == "__main__":
    unittest.main()
