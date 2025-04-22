import json
import math
import sys
import os
import logging
from time import sleep
from dotenv import load_dotenv
from psycopg2 import Error
from psycopg2 import pool
import fastfeedparser
import requests
from sqs_queue import SQSQueue
import trafilatura
import re
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scraper.log')
    ]
)
logger = logging.getLogger(__name__)

class Scraper:
    def __init__(self):
        load_dotenv()
        self.connection_pool: pool.ThreadedConnectionPool 
        self.init_pool()
        self.init_db()        
        self.sqs_queue = SQSQueue()
        self.robots_cache = {}

    def init_pool(self):
        try:
            self.connection_pool = pool.ThreadedConnectionPool(
                minconn=1,  
                maxconn=10, 
                host=os.getenv('PGHOST'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port="5432"
            )
        except (Exception, Error) as error:
            logger.error("Error while creating connection pool: %s", error)
            sys.exit(1)

    def get_connection(self):
        if self.connection_pool is None:
            self.init_pool()
        return self.connection_pool.getconn()

    def release_connection(self, connection):
        if self.connection_pool is not None:
            self.connection_pool.putconn(connection)

    def close_pool(self):
        if self.connection_pool is not None:
            self.connection_pool.closeall()

    def __del__(self):
        self.close_pool()
    
    def init_db(self):
        conn = self.get_connection()
        try:
            filepath = os.path.join(os.path.dirname(__file__), '..', 'db', 'schema.sql')
            with open(filepath, 'r') as f:
                schema = f.read()
                with conn.cursor() as cur:
                    cur.execute(schema)
                conn.commit()
        except (Exception, Error) as error:
            logger.error("Error while initializing database: %s", error)
            sys.exit(1)
        finally:
            self.release_connection(conn)
    
    def update_feeds_list(self):
        smallweb_feeds = self.get_smallweb_feeds()

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT feed_url FROM feeds")
            existing_feeds = {row[0] for row in cursor.fetchall()}        
            new_feeds = [feed for feed in smallweb_feeds if feed not in existing_feeds]

            logger.info("Found %d existing feeds", len(existing_feeds))
            logger.info("Found %d new feeds", len(new_feeds))
            
            if new_feeds:
                insert_query = "INSERT INTO feeds (feed_url) VALUES (%s) ON CONFLICT DO NOTHING"
                cursor.executemany(insert_query, [(feed,) for feed in new_feeds])
                conn.commit()
                logger.info("Added %d new feeds to the database", len(new_feeds))
            else:
                logger.info("No new feeds to add")
            
            cursor.close()
        except Exception as e:
            conn.rollback()
            logger.error("Error updating feeds list: %s", e)
        finally:
            self.release_connection(conn)

    def get_smallweb_feeds(self) -> list[str]:
        feeds_file_url = 'https://raw.githubusercontent.com/kagisearch/smallweb/refs/heads/main/smallweb.txt'
        try:
            response = requests.get(feeds_file_url)
            response.raise_for_status()  
            
            feeds = [line.strip().rstrip('/') for line in response.text.splitlines() if line.strip()]
            return list(set(feeds))
        
        except Exception as e:
            logger.error("Error downloading feeds file: %s", e)
            return []
    
    def get_all_feeds(self, only_due_for_update: bool = False):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if only_due_for_update:
                cursor.execute("SELECT feed_url FROM feeds WHERE last_check_date < CURRENT_DATE - INTERVAL '1 day'")
            else:
                cursor.execute("SELECT feed_url FROM feeds")
            feeds = [row[0] for row in cursor.fetchall()]
            return feeds
        except (Exception, Error) as error:
            logger.error("Error getting all feeds: %s", error)
            return []
        finally:
            self.release_connection(conn)
    
    def mark_feed_as_checked(self, feed_url: str):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE feeds SET last_check_date = CURRENT_DATE WHERE feed_url = %s", (feed_url,))
            conn.commit()
        except (Exception, Error) as error:
            logger.error("Error marking feed as checked: %s", error)
        finally:
            self.release_connection(conn)

    def scrape(self):
        #self.enqueue_new_feed_entries()
        while True:
            self.process_queue_message()
    
    def url_exists_in_db(self, url: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pages WHERE url = %s)", (url,))
            exists = cursor.fetchone()[0]
            cursor.close()
            return exists
        except (Exception, Error) as error:
            logger.error("Error checking URL existence: %s", error)
            return True
        finally:
            self.release_connection(conn)
    
    def is_blog_post_url(self, url: str) -> bool:
        non_blog_patterns = [
            r'^.*/(about|links|tags|categories|archive|contact)/?$',  
            r'^.*/(author|tag|category)/[^/]+/?$',                    
            r'^.*/(tag|category)$'                                   
        ]
        
        for pattern in non_blog_patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return False
        
        return True

    def calculate_visibility_timeout(self, num_urls: int) -> int:
        timout = 6
        scrape_delay = 6
        processing_delay = 2
        safety_margin = 1.2
        return math.ceil(safety_margin * (num_urls * (timout + scrape_delay + processing_delay)))
    
    def process_feed(self, feed):
        thread_name = threading.current_thread().name
        logger.info("[%s] Processing feed: %s", thread_name, feed)
        try:
            new_urls = []
            parsed_feed = fastfeedparser.parse(feed)
            link = parsed_feed.feed.link
            logger.info("[%s] Feed link: %s", thread_name, link)
            logger.info("[%s] Found %d entries", thread_name, len(parsed_feed.entries))
        except Exception as e:
            logger.error("[%s] Error parsing feed %s: %s", thread_name, feed, e)
            return

        try:
            for entry in parsed_feed.entries:
                if hasattr(entry, 'link'):
                    entry_link = entry.link.strip().rstrip('/')
                    if not self.is_blog_post_url(entry_link):
                        logger.debug("[%s] Skipping non-blog URL: %s", thread_name, entry_link)
                        continue
                    exists = self.url_exists_in_db(entry_link)
                    if not exists:
                        new_urls.append(entry_link)
                    logger.debug("[%s] URL %s: %s", thread_name, entry_link, 'exists' if exists else 'new')
        except Exception as e:
            logger.error("[%s] Error processing feed %s: %s", thread_name, feed, e)
            return

        logger.info("[%s] Found %d new URLs for feed %s", thread_name, len(new_urls), feed)
        if new_urls:
            self.sqs_queue.send_message(new_urls)
            logger.info("[%s] Sent %d new URLs to the queue", thread_name, len(new_urls))
            self.mark_feed_as_checked(feed)
    
    def enqueue_new_feed_entries(self):
        feeds = self.get_all_feeds(only_due_for_update=False) #True in prod
        feeds = feeds[6000:6005] # subset of feeds for testing
        
        max_workers = 4
        
        logger.info("\nProcessing %d feeds with %d worker threads\n", len(feeds), max_workers)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executor.map(self.process_feed, feeds)
            
        logger.info("All feeds processed")

    def process_queue_message(self):
        message = self.sqs_queue.receive_message()
        if not message:
            logger.info("No message received")
            return

        try:
            receipt_handle = message['ReceiptHandle']
            body = json.loads(message['Body'])
            urls = body['urls']
        except Exception as e:
            logger.error("Error parsing message: %s", e)
            return

        if len(urls) > 1:
            visibility_timeout = self.calculate_visibility_timeout(len(urls))
            self.sqs_queue.change_message_visibility(receipt_handle, visibility_timeout)
            logger.info("Changed message visibility to %d seconds", visibility_timeout)
        
        urls_to_scrape = []
        for url in urls:
            if self.is_url_in_db(url):
                logger.debug("URL already in database: %s", url)
            else:
                urls_to_scrape.append(url)
        
        max_workers = 4  
        logger.info("Processing %d URLs with %d worker threads", len(urls_to_scrape), max_workers)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self.scrape_url_with_delay, url) for url in urls_to_scrape]
            
            for future in futures:
                try:
                    future.result()  
                except Exception as e:
                    logger.error("Error in thread: %s", e)

        self.sqs_queue.delete_message(receipt_handle)
        logger.info("Deleted message")
        sleep(10)
    
    def scrape_url_with_delay(self, url):
        thread_name = threading.current_thread().name
        try:
            logger.info("[%s] Scraping URL: %s", thread_name, url)
            self.scrape_url(url)
        except Exception as e:
            logger.error("[%s] Error scraping URL: %s", thread_name, e)
        finally:
            sleep(6)
    
    def scrape_url(self, url: str):
        thread_name = threading.current_thread().name
        parsed_url = urlparse(url)
        domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
        if not self.robots_allows_scraping(domain, url):
            logger.warning("[%s] Robots.txt disallows scraping: %s", thread_name, url)
            return
        try:
            downloaded_content = trafilatura.fetch_url(url)
        except Exception as e:
            logger.error("[%s] Error downloading content for %s: %s", thread_name, url, e)
            return
        if not downloaded_content:
            logger.warning("[%s] Failed to download content for %s", thread_name, url)
            return
        
        if not trafilatura.readability_lxml.is_probably_readerable(downloaded_content):
            logger.warning("[%s] Downloaded content for %s is not readable", thread_name, url)
            return
        
        extracted = trafilatura.extract(downloaded_content, output_format='json', with_metadata=True)
        if not extracted:
            logger.warning("[%s] Failed to extract text for %s", thread_name, url)
            return
        
        extracted_dict = json.loads(extracted)
        if len(extracted_dict['raw_text'].split()) < 100:
            logger.warning("[%s] Extracted text for %s is too short", thread_name, url)
            return
        
        page = {
            'title': extracted_dict['title'],
            'url': url,
            'fingerprint': extracted_dict['fingerprint'],
            'date': extracted_dict['date'],
            'text': extracted_dict['raw_text']
        }
        self.save_page(page)
        logger.info("[%s] Successfully processed and saved page: %s", thread_name, url)
    
    def save_page(self, page: dict):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO pages (title, url, fingerprint, date, text) VALUES (%s, %s, %s, %s, %s)", (page['title'], page['url'], page['fingerprint'], page['date'], page['text']))
            conn.commit()
        except (Exception, Error) as error:
            logger.error("Error saving page: %s", error)
        finally:
            self.release_connection(conn)
        
    def is_url_in_db(self, url: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pages WHERE url = %s)", (url,))
            exists = cursor.fetchone()[0]
            cursor.close()
            return exists
        except (Exception, Error) as error:
            logger.error("Error checking URL existence: %s", error)
            return True
        finally:
            self.release_connection(conn)
    
    def robots_allows_scraping(self, domain: str, url: str) -> bool:
        if domain not in self.robots_cache:
            rp = RobotFileParser()
            robots_url = f"{domain}/robots.txt"
            try:
                rp.set_url(robots_url)
                rp.read()
                self.robots_cache[domain] = rp
            except Exception as e:
                logger.warning("Error fetching robots.txt for %s (continuing with scrape): %s", domain, e)
                return True
                
        rp = self.robots_cache[domain]
        return rp.can_fetch("*", url)

    def tmp(self):
        feeds = self.get_all_feeds(only_due_for_update=False)
        logger.info("Total feeds: %d", len(feeds))



if __name__ == "__main__":
    scraper = Scraper()
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "update_feeds":
            scraper.update_feeds_list()
        elif command == "enqueue":
            scraper.enqueue_new_feed_entries()
        elif command == "scrape":
            scraper.scrape()
        elif command == "tmp":
            scraper.tmp()
        else:
            logger.error("Unknown command: %s", command)
            logger.info("Available commands: update_feeds, enqueue, scrape, tmp")
    else:
        logger.info("Available commands: update_feeds, enqueue, scrape, tmp")


