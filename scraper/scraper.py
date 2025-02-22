import requests
from urllib.parse import urlparse
import fastfeedparser
import re
from trafilatura.sitemaps import sitemap_search


class Scraper:
    def __init__(self) -> None:
        pass

    def start_initial_scrape(self):
        """Download a list of feed URLs from a remote source"""
        smallweb_feeds_url = "https://raw.githubusercontent.com/kagisearch/smallweb/refs/heads/main/smallweb.txt"
        response = requests.get(smallweb_feeds_url)
        if response.status_code == 200:
            feeds = response.text.splitlines()
        else:
            raise Exception(f"Failed to download smallweb feeds: {response.status_code}")
        
        return feeds[12544:12549]

    def get_url_path_components(self, url):
        parsed_path = urlparse(url)
        path_elements = [x for x in parsed_path.path.split('/') if x]
        return path_elements

    def get_sample_urls(self, feed):
        try:
            parsed_feed = fastfeedparser.parse(feed)
        except Exception as e:
            print(f"Failed to parse feed: {feed}. Exception: {e}")
            return []
        
        urls = []
        for entry in parsed_feed.entries[:5]:
            urls.append(entry)
        
        return urls


    def get_sitemap(self, domain):
        try:
            print(f"Searching sitemap for {domain}")
            # sitemap_search returns an iterator; convert it to a list.
            return list(sitemap_search(str(domain)))
        except (Exception, requests.RequestException) as e:
            print(f"Error searching sitemap for {domain}: {e}")
            return []

    # def get_url_pattern(self, urls):
    #     """
    #     Extract the common URL pattern from a list of known blog post URLs.
    #     Returns a regex pattern that matches similar URLs.
    #     """
    #     patterns = []
    #     for url in urls:
    #         path_components = self.get_url_path_components(url)
            
    #         # Skip empty paths
    #         if not path_components:
    #             continue
                
    #         # Replace date components with regex patterns
    #         processed_components = []
    #         for component in path_components:
    #             # Match year patterns (2020, 2021, etc.)
    #             if re.match(r'^20\d{2}$', component):
    #                 processed_components.append(r'20\d{2}')
    #             # Match month patterns (01-12)
    #             elif re.match(r'^(0[1-9]|1[0-2])$', component):
    #                 processed_components.append(r'(0[1-9]|1[0-2])')
    #             # Match day patterns (01-31)
    #             elif re.match(r'^(0[1-9]|[12]\d|3[01])$', component):
    #                 processed_components.append(r'(0[1-9]|[12]\d|3[01])')
    #             else:
    #                 processed_components.append('[\w-]+')
            
    #         pattern = '/'.join(processed_components)
    #         patterns.append(pattern)
        
    #     # Combine patterns with OR operator if multiple patterns exist
    #     return '|'.join(f'^/{p}$' for p in set(patterns))

    # def identify_blog_posts(self, feed_url):
    #     """
    #     Identify blog post URLs for a given feed by:
    #     1. Getting sample URLs from RSS feed
    #     2. Extracting the URL pattern
    #     3. Matching sitemap URLs against this pattern (or using RSS feed URLs if no sitemap)
    #     """
    #     # Get domain from feed URL
    #     domain = urlparse(feed_url).netloc
    #     if not domain:
    #         return []
        
    #     # Get sample blog post URLs from RSS feed
    #     sample_urls = self.get_sample_urls(feed_url)
    #     if not sample_urls:
    #         return []
        
    #     # Extract URL pattern from sample URLs
    #     pattern = self.get_url_pattern(sample_urls)
    #     if not pattern:
    #         return []
        
    #     # Try to get URLs from sitemap first
    #     sitemap_urls = self.get_sitemap(domain)
        
    #     # If no sitemap URLs found, use all URLs from the RSS feed
    #     urls_to_check = sitemap_urls if sitemap_urls else sample_urls
        
    #     # Match URLs against the pattern
    #     blog_posts = []
    #     for url in urls_to_check:
    #         path_components = self.get_url_path_components(url)
    #         path = '/' + '/'.join(path_components)
    #         if re.match(pattern, path):
    #             blog_posts.append(url)
        
    #     return blog_posts


if __name__ == "__main__":
    scraper = Scraper()
    smallweb_feeds = scraper.start_initial_scrape()
    
    for feed in smallweb_feeds:
        print(f"\nProcessing feed: {feed}")
        sample_urls = scraper.get_sample_urls(feed)
        print(f"Found {len(sample_urls)} sample urls: {sample_urls}")
        # blog_posts = scraper.identify_blog_posts(feed)
        # print(f"Found {len(blog_posts)} blog posts")
        # print(f"Sample posts: {blog_posts[:3]}")
        
        
        print("\n")
