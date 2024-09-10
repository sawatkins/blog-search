import asyncio
import aiohttp
import feedparser
from bs4 import BeautifulSoup
import json
from h11 import Response
from playwright.async_api import async_playwright # type: ignore
import os
import sqlite3
from courlan import clean_url
import trafilatura
import trafilatura.sitemaps
from trafilatura.readability_lxml import is_probably_readerable
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse
import time
from collections import defaultdict
import random

# Add this near the top of your file, with other imports and constants
TESTING_MODE = True  # Set to False for normal operation

async def extract_content_with_readability(page, url):
    await page.goto(url, timeout=30000) 
    
    # Read the local Readability.js file
    readability_js_path = os.path.join(os.path.dirname(__file__), 'Readability.min.js')
    
    # Add Readability.js to the page
    await page.add_script_tag(path=readability_js_path)
    
    # Now use Readability to extract the content, title, and date
    return await page.evaluate('''() => {
        var article = new Readability(document).parse();
        if (article) {
            return {
                title: article.title,
                content: article.textContent,
                date: article.datePublished || ''
            };
        }
        return null;
    }''')

# Remove HTML tags and clean text
def clean_text(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator=' ', strip=True)

# Read URLs from a file
def read_urls(file_path):
    with open(file_path, 'r') as file:
        return [line.strip() for line in file]

# Print a sample of posts
def print_sample_posts(posts, sample_size=10):
    print(f"\nTotal posts found: {len(posts)}")
    print(f"\nSample of {sample_size} posts:")
    for post in posts[:sample_size]:
        print(f"Title: {post['title']}")
        print(f"URL: {post['url']}")
        print(f"Date: {post['date']}")
        print(f"Content: {post['content'][:100]}...")
        print()

# Add this new function to save posts to a JSON file
def save_posts_to_json(posts, filename="all_posts.json"):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(posts, f, ensure_ascii=False, indent=4)
    print(f"Saved {len(posts)} posts to {filename}")

def init_db(db_name='posts.db'):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        url TEXT UNIQUE,
        date TEXT,
        content TEXT
    )
    ''')
    conn.commit()
    return conn

def load_posts_from_json(filename="all_posts.json"):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)
    
    
def save_posts_to_db(posts, conn):
    cursor = conn.cursor()
    for post in posts:
        cursor.execute('''
        INSERT OR REPLACE INTO posts (title, url, date, content)
        VALUES (?, ?, ?, ?)
        ''', (post['title'], post['url'], post['date'], post['content']))
    conn.commit()
    print(f"Saved {len(posts)} posts to the database")
    
def check_robots_txt(domain):
    robot_parser = RobotFileParser(f"{domain}/robots.txt")
    robot_parser.read()
    return robot_parser.can_fetch("*", domain)

# Modify the main function
async def main():
    feeds = read_urls("small-feeds.txt")
    cleaned_domains = [clean_url(feedparser.parse(url).feed.link) for url in feeds]
    
    print('Initial cleaned_domains:', len(cleaned_domains))
    allowed_domains = [domain for domain in cleaned_domains 
                       if check_robots_txt(domain)]
    
    print('Allowed domains:', len(allowed_domains))
    print(allowed_domains)
    
    sitemap_links = list(set(sitemap for domain in allowed_domains 
                             for sitemap in trafilatura.sitemaps.sitemap_search(str(domain))))
    
    print('sitemap_links:', len(sitemap_links))
    
    all_posts = []
    last_request_time = defaultdict(float)
    processed_domains = set() # for testing
    
    DELAY_BETWEEN_REQUESTS = 5  # seconds
    MAX_CONCURRENT_REQUESTS = 10
    MAX_URLS = 10 if TESTING_MODE else float('inf')  # Limit only when testing

    async with async_playwright() as p:
        browser = await p.firefox.launch()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def process_url(url):
            async with semaphore:
                if len(all_posts) >= MAX_URLS:
                    return  # Stop processing if we've reached the limit for testing

                domain = urlparse(url).netloc
                # if TESTING_MODE and domain in processed_domains:
                #     return  # Skip if we've already processed this domain (only when testing)

                time_since_last_request = time.time() - last_request_time[domain]
                if time_since_last_request < DELAY_BETWEEN_REQUESTS:
                    await asyncio.sleep(DELAY_BETWEEN_REQUESTS - time_since_last_request)
                
                try:
                    page = await browser.new_page()
                    result = await extract_content_with_readability(page, url)
                    await page.close()
                    if result:
                        post = {
                            'title': result['title'],
                            'url': url,
                            'date': result['date'],
                            'content': clean_text(result['content'])
                        }
                        all_posts.append(post)
                        processed_domains.add(domain) # for testing
                        print(f"Content extracted from URL: {url}")
                    else:
                        print(f"No content extracted from URL: {url}")
                except Exception as e:
                    print(f"Error processing URL {url}: {e}")
                
                last_request_time[domain] = time.time()

        if TESTING_MODE:
            # Shuffle the sitemap_links to get a random mix of domains when testing
            random.shuffle(sitemap_links)
            
            for url in sitemap_links:
                if len(all_posts) >= MAX_URLS:
                    break
                await process_url(url)
        else:
            # Process all URLs when not in testing mode
            await asyncio.gather(*[process_url(url) for url in sitemap_links])
        
        await browser.close()
    
    # Save posts to JSON file
    save_posts_to_json(all_posts)
    
    # Optionally save to SQLite database
    conn = init_db()
    save_posts_to_db(all_posts, conn)
    conn.close()
    
    # print_sample_posts(all_posts)



def new_main():
    feeds = read_urls("small-feeds.txt")
    
    # get the cleaned domains
    cleaned_domains = []
    for url in feeds:
        feed = feedparser.parse(url)
        cleaned_domain = clean_url(feed.feed.link)
        cleaned_domains.append(cleaned_domain)
    print('cleaned_domains', len(cleaned_domains))
    print(cleaned_domains)
    
    # get allowed domains
    allowed_domains = []
    for domain in cleaned_domains:
        if check_robots_txt(domain):
            allowed_domains.append(domain)
    print('allowed_domains', len(allowed_domains))
    print(allowed_domains)
    
    # get the sitemap links
    sitemap_links = list(set(sitemap for domain in allowed_domains 
                             for sitemap in trafilatura.sitemaps.sitemap_search(str(domain))))
    print('sitemap_links', len(sitemap_links))

    test_sitemap_link = ['https://jakeseliger.com/2017/06/27/violence-and-the-sacred-on-campus/',
                         'https://letterstoanewdeveloper.com/2019/07/29/learn-a-little-jq-awk-and-sed/',
                         'https://jakeseliger.com/2015/12/14/on-the-manosphere-or-red-pill/',
                         'https://letterstoanewdeveloper.com/2019/08/05/subscribe-to-a-weekly-link-newsletter/',
                         'https://letterstoanewdeveloper.com/2022/03/07/changes-from-bootcamp-to-a-real-dev-job/',
                         'https://jakeseliger.com/2021/02/03/the-effect-of-zoning-restrictions-on-the-life-of-the-artist/']

    # extrace title, link, date, author, and content from each of these sitemap links
    # list of URLs
    
    # number of threads to use
    threads = 4

    # converted the input list to an internal format
    url_store = trafilatura.downloads.add_to_compressed_dict(test_sitemap_link)
    
    extracted_content = []

    # processing loop
    while url_store.done is False:
        bufferlist, url_store = trafilatura.downloads.load_download_buffer(url_store, sleep_time=5)
        # process downloads
        for url, result in trafilatura.downloads.buffered_downloads(bufferlist, threads):
            print('scraping:', url)
            if result is not None:
                if is_probably_readerable(result):
                    extracted = trafilatura.extract(result, output_format='json', with_metadata=True)
                    if extracted is not None:
                        # print('extracted', extracted)
                        # make extracted (which is a json string) somehting that i can extarct values from
                        extracted_dict = json.loads(extracted)
                        page = {
                            'title': extracted_dict['title'],
                            'link': clean_url(url),
                            'fingerprint': extracted_dict['fingerprint'],
                            'date': extracted_dict['date'],
                            'text': extracted_dict['raw_text']
                        }
                        extracted_content.append(page)
                    else:
                        print('Failed to extract content from', url)
            else:
                print(f"Failed to download {url}")

    # Save the extracted content to a JSON file
    print('Saving extracted content to a file')
    with open('extracted_content.json', 'w', encoding='utf-8') as f:
        json.dump(extracted_content, f, ensure_ascii=False, indent=4)
    
    #Print the results
    # for sitemap_link in sitemap_links:
    #     print(f"Sitemap URL: {sitemap_link}")

if __name__ == "__main__":
    # asyncio.run(main())
    new_main()
