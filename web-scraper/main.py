from datetime import datetime
import queue
import time
import json
import sqlite3 
from feedparser import parse
from courlan import clean_url
from trafilatura import extract
from trafilatura.sitemaps import sitemap_search
from trafilatura.downloads import add_to_compressed_dict, load_download_buffer, buffered_downloads
from trafilatura.readability_lxml import is_probably_readerable
from urllib.robotparser import RobotFileParser
import threading
import requests
from urllib.error import URLError

def read_urls(file_path):
    with open(file_path, 'r') as file:
        return [line.strip() for line in file]

def save_posts_to_json(posts, filename="pages.json"):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(posts, f, ensure_ascii=False, indent=4)
    print(f"Saved {len(posts)} posts to {filename}")
    
def load_posts_from_json(filename="all_posts.json"):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)

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
       
def save_all_posts_to_db(pages, conn):
    cursor = conn.cursor()
    for page in pages:      
        cursor.execute('''
        INSERT OR REPLACE INTO pages (title, url, fingerprint, date, text, scraped_on_date)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (page['title'], page['url'], page['fingerprint'], page['date'], page['text'], page['scraped_on_date']))
    conn.commit()
    print(f"Saved {len(pages)} pages to the database")
    
def check_robots_txt(domain):
    robot_parser = RobotFileParser()
    try:
        robot_parser.set_url(f"{domain}/robots.txt")
        robot_parser.read()
        can_crawl = robot_parser.can_fetch("*", domain)
        if not can_crawl:
            print(f"should not crawl {domain}")
        return can_crawl
    except (URLError, requests.RequestException) as e:
        return True
    except Exception as e:
        print(f"error when checking robots.txt for {domain}: {type(e).__name__}.")
        return False

def consumer_thread(extracted_content, db_name, stop_event):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    while not stop_event.is_set() or not extracted_content.empty():
        try:
            page = extracted_content.get(timeout=1)
            cursor.execute('''
            INSERT OR REPLACE INTO pages (title, url, fingerprint, date, text, scraped_on_date)
            VALUES (?, ?, ?, ?, ?, ?)
            ''', (page['title'], page['url'], page['fingerprint'], page['date'], page['text'], page['scraped_on_date']))
            conn.commit()
            extracted_content.task_done()
        except queue.Empty:
            continue
    conn.close()
    print("Consumer thread finished")

def main():
    feeds = read_urls("feed_urls.txt")
    
    
    # read cleaned domains from file
    cleaned_domains = read_urls("cleaned_domains.txt")
    # get the cleaned domains
    # cleaned_domains = []
    # for url in feeds:
    #     try:
    #         feed = parse(url)
    #         cleaned_domain = clean_url(feed.feed.link)
    #     except:
    #         print(f"error getting domain from feed: {url}")
    #         # TODO: keep list of error and failed feeds
    #         continue
    #     # TODO: validate that the doamin exists and is valid
    #     cleaned_domains.append(cleaned_domain)
    print('cleaned_domains', len(cleaned_domains))
    print(cleaned_domains)
    
    # get allowed domains from robots.txt
    allowed_domains = []
    for domain in cleaned_domains:
        if check_robots_txt(domain):
            allowed_domains.append(domain)
    print('allowed_domains', len(allowed_domains))
    print(allowed_domains)
    
    # get the sitemap links
    sitemap_links = list(set(sitemap for domain in allowed_domains 
                             for sitemap in sitemap_search(str(domain))))
    print('sitemap_links', len(sitemap_links))

    test_sitemap_link = ['https://jakeseliger.com/2017/06/27/violence-and-the-sacred-on-campus/',
                         'https://letterstoanewdeveloper.com/2019/07/29/learn-a-little-jq-awk-and-sed/',
                         'https://jakeseliger.com/2015/12/14/on-the-manosphere-or-red-pill/',
                         'https://letterstoanewdeveloper.com/2019/08/05/subscribe-to-a-weekly-link-newsletter/',
                         'https://letterstoanewdeveloper.com/2022/03/07/changes-from-bootcamp-to-a-real-dev-job/',
                         'https://jakeseliger.com/2021/02/03/the-effect-of-zoning-restrictions-on-the-life-of-the-artist/']

    db_name = 'posts.db'
    init_db(db_name)  # Just initialize the database
    extracted_content = queue.Queue()
    stop_event = threading.Event()
    consumer = threading.Thread(target=consumer_thread, args=(extracted_content, db_name, stop_event))
    consumer.start()

    threads = 10
    url_store = add_to_compressed_dict(sitemap_links) # test_sitemap_link
    while url_store.done is False:
        bufferlist, url_store = load_download_buffer(url_store, sleep_time=5)
        for url, result in buffered_downloads(bufferlist, threads):
            print('scraping:', url)
            if result is not None:
                if is_probably_readerable(result):
                    extracted = extract(result, output_format='json', with_metadata=True)
                    if extracted is not None:
                        extracted_dict = json.loads(extracted)
                        page = {
                            'title': extracted_dict['title'],
                            'url': clean_url(url), # clean_url maybe not needed?
                            'fingerprint': extracted_dict['fingerprint'],
                            'date': extracted_dict['date'],
                            'text': extracted_dict['raw_text'],
                            'scraped_on_date': datetime.now().strftime("%Y-%m-%d")
                        }
                        extracted_content.put(page)
                    else:
                        print('Failed to extract content from', url)
            else:
                print(f"Failed to download {url}")

    stop_event.set()
    consumer.join()
    print('consumer joined')

if __name__ == "__main__":
    start_scraping = input("Are you sure you want to start scraping? (yes/no): ")
    if start_scraping.lower() != "yes":
        print("Scraping cancelled.")
        exit()
    start_time = time.time()
    main()
    end_time = time.time()
    print(f"Time taken: {round(end_time - start_time)} seconds")
