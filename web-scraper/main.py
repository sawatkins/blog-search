from datetime import datetime
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
    robot_parser = RobotFileParser(f"{domain}/robots.txt")
    robot_parser.read()
    # TODO: return true if no robots.txt is found
    return robot_parser.can_fetch("*", domain)

def main():
    feeds = read_urls("small-feeds.txt")
    
    # get the cleaned domains
    cleaned_domains = []
    for url in feeds:
        feed = parse(url)
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
                             for sitemap in sitemap_search(str(domain))))
    print('sitemap_links', len(sitemap_links))

    test_sitemap_link = ['https://jakeseliger.com/2017/06/27/violence-and-the-sacred-on-campus/',
                         'https://letterstoanewdeveloper.com/2019/07/29/learn-a-little-jq-awk-and-sed/',
                         'https://jakeseliger.com/2015/12/14/on-the-manosphere-or-red-pill/',
                         'https://letterstoanewdeveloper.com/2019/08/05/subscribe-to-a-weekly-link-newsletter/',
                         'https://letterstoanewdeveloper.com/2022/03/07/changes-from-bootcamp-to-a-real-dev-job/',
                         'https://jakeseliger.com/2021/02/03/the-effect-of-zoning-restrictions-on-the-life-of-the-artist/']

    threads = 10
    url_store = add_to_compressed_dict(sitemap_links) # test_sitemap_link
    extracted_content = []
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
                        extracted_content.append(page)
                    else:
                        print('Failed to extract content from', url)
            else:
                print(f"Failed to download {url}")

    save_posts_to_json(extracted_content, 'extracted_content.json')    
    conn = init_db()
    save_all_posts_to_db(extracted_content, conn)
    conn.close()

if __name__ == "__main__":
    start_scraping = input("Do you want to start scraping? (yes/no): ")
    if start_scraping.lower() != "yes":
        print("Scraping cancelled.")
        exit()
    start_time = time.time()
    main()
    end_time = time.time()
    print(f"Time taken: {end_time - start_time} seconds")
