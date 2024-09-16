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
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from requests.exceptions import RequestException

def read_urls(file_path):
    with open(file_path, 'r') as file:
        return [line.strip() for line in file]

def save_urls(urls, filename):
    with open(filename, 'w') as file:
        for url in urls:
            file.write(url + '\n')

def save_posts_to_json(posts, filename="pages.json"):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(posts, f, ensure_ascii=False, indent=4)
    print(f"Saved {len(posts)} posts to {filename}")
    
def load_posts_from_json(filename="all_posts.json"):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)

def init_db(db_name='pages.db'):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        url TEXT,
        fingerprint TEXT UNIQUE,
        date TEXT,
        text TEXT,
        scraped_on_date TEXT
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
        # TODO: still return true if there is no robots.txt,
        # but dont if the website is unreachable
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

def search_sitemap(domain):
    try:
        print(f"Searching sitemap for {domain}")
        return list(sitemap_search(str(domain)))  # Remove the non-existent timeout parameter
    except (Exception, RequestException) as e:
        print(f"Error searching sitemap for {domain}: {e}")
        return []

def get_sitemap_links(allowed_domains, max_workers=20, timeout=30):
    # TODO: take another look at this code
    sitemap_links = set()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_domain = {executor.submit(search_sitemap, domain): domain for domain in allowed_domains}
        for future in as_completed(future_to_domain):  # Remove timeout from as_completed
            domain = future_to_domain[future]
            try:
                result = future.result(timeout=timeout)  # Apply timeout to individual future
                sitemap_links.update(result)
                print(f"Processed {domain}: {len(result)} links")
            except concurrent.futures.TimeoutError:
                print(f"Timeout for {domain}")
            except Exception as e:
                print(f"Exception for {domain}: {e}")
    
    return list(sitemap_links)

def get_sitemap_links_new_consumer(sitemap_links_queue, stop_event):
    print("get_sitemap_links_new_consumer started")
    while not stop_event.is_set():
        try:
            sitemap_links = sitemap_links_queue.get(timeout=2)
            with open('sitemap_links.txt', 'a') as file:
                for link in sitemap_links:
                    file.write(link + '\n')
            print(f"added {len(sitemap_links)} sitemap links to file")
        except queue.Empty:
            continue

def get_sitemap_links_new(allowed_domains):
    sitemap_links_queue = queue.Queue()
    stop_event = threading.Event()
    consumer = threading.Thread(target=get_sitemap_links_new_consumer, args=(sitemap_links_queue, stop_event))
    consumer.start()

    def process_domain(domain):
        try:
            print(f"Processing {domain}")
            links = search_sitemap(domain)
            if links:
                sitemap_links_queue.put(links)
        except Exception as e:
            print(f"Error processing {domain}: {e}")

    with ThreadPoolExecutor(max_workers=20) as executor:
        executor.map(process_domain, allowed_domains)
    
    # TODO: consumer should exit when queue is empty
    stop_event.set()
    print("get_sitemap_links_new_consumer stopping")
    consumer.join()
    print("get_sitemap_links_new_consumer joined")

    return read_urls('sitemap_links.txt')

def get_db_urls(db_name):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('SELECT url FROM pages')
    db_urls = [row[0] for row in cursor.fetchall()]
    db_urls = [clean_url(url) for url in db_urls]
    conn.close()
    return db_urls


def main():
    # feeds = read_urls("feed_urls.txt")
    
    
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
    # print(cleaned_domains)
    
    allowed_domains = read_urls("allowed_domains.txt")
    # get allowed domains from robots.txt
    # allowed_domains = []
    # for domain in cleaned_domains:
    #     if check_robots_txt(domain):
    #         allowed_domains.append(domain)
    print('allowed_domains', len(allowed_domains))
    # save allowed domains to file
    # save_urls(allowed_domains, 'allowed_domains.txt')
    # print(allowed_domains)
    
    # get the sitemap links
    # TODO: make this significantly faster
    # TODO: handle connection errors and timeouts
    # sitemap_links = list(set(sitemap for domain in allowed_domains 
    #                          for sitemap in sitemap_search(str(domain))))
    # sitemap_links = []
    # for domain in allowed_domains:
    #     try:
    #         links = sitemap_search(domain)
    #         sitemap_links.append(links)
    #         print(f"got sitemap for {domain}")
    #     except:
    #         print(f"error getting sitemap for {domain}")
    # print('sitemap_links', len(sitemap_links))
    # sitemap_links = get_sitemap_links(allowed_domains)
    # sitemap_links = get_sitemap_links_new(allowed_domains)
    sitemap_links = read_urls('sitemap_links.txt')
    print('sitemap_links', len(sitemap_links))
    # save_urls(sitemap_links, 'sitemap_links.txt')

    # test_sitemap_link = ['https://jakeseliger.com/2017/06/27/violence-and-the-sacred-on-campus/',
    #                      'https://letterstoanewdeveloper.com/2019/07/29/learn-a-little-jq-awk-and-sed/',
    #                      'https://jakeseliger.com/2015/12/14/on-the-manosphere-or-red-pill/',
    #                      'https://letterstoanewdeveloper.com/2019/08/05/subscribe-to-a-weekly-link-newsletter/',
    #                      'https://letterstoanewdeveloper.com/2022/03/07/changes-from-bootcamp-to-a-real-dev-job/',
    #                      'https://jakeseliger.com/2021/02/03/the-effect-of-zoning-restrictions-on-the-life-of-the-artist/']

    # print('exiting before actual scraping')
    # exit()

    db_name = 'pages.db'
    init_db(db_name)  # Just initialize the database
    
    db_urls = get_db_urls(db_name)
    print('existingdb_urls', len(db_urls))
    
    extracted_content = queue.Queue()
    stop_event = threading.Event()
    consumer = threading.Thread(target=consumer_thread, args=(extracted_content, db_name, stop_event))
    consumer.start()

    
    threads = 20
    url_store = add_to_compressed_dict(sitemap_links) # test_sitemap_link
    while url_store.done is False:
        bufferlist, url_store = load_download_buffer(url_store, sleep_time=5)
        for url, result in buffered_downloads(bufferlist, threads):
            print('scraping:', url)
            if result is not None and clean_url(url) not in db_urls:
                try:
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
                        print(f"Content not readable for {url}")
                except Exception as e:
                    print(f"Error checking readability for {url}: {str(e)}")
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
