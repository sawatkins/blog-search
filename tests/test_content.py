import gzip
import unittest

from scraper.content import archive_jobs, extract_page, feed_jobs, sitemap_jobs, source_jobs


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

    def test_short_post_is_not_rejected_for_having_under_100_words(self):
        body = b'<html><head><title>A walk</title></head><body><article><h1>A walk</h1><p>Today I walked along the river and watched the birds gathering on the old bridge. A small, peaceful moment I wanted to remember.</p></article></body></html>'
        page = extract_page(body, "https://example.org/?p=1")
        self.assertIsNotNone(page)
        self.assertIn("river", page["text"])
        self.assertLess(len(page["text"].split()), 100)
        self.assertEqual(page["url"], "https://example.org/?p=1")

    def test_articles_under_posts_are_indexed_not_only_traversed(self):
        tasks = archive_jobs(b'<html><a href="/posts/">All posts</a><a href="/posts/2004/old-post/">Old post</a></html>',
                             "https://example.org/", "https://example.org/")
        self.assertEqual({(t["kind"], t["url"]) for t in tasks}, {
            ("archive", "https://example.org/posts/"),
            ("page", "https://example.org/posts/2004/old-post/"),
        })


if __name__ == "__main__":
    unittest.main()
