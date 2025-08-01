import json
import logging
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from time import sleep
from urllib.robotparser import RobotFileParser

import fastfeedparser
import requests
import trafilatura
from courlan import clean_url, get_base_url, is_valid_url
from dotenv import load_dotenv
from psycopg2 import Error, pool

from sqs_queue import SQSQueue

MAX_WORKERS = 99

def setup_logger():
    logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(logs_dir, f'{timestamp}_scraper.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logger()

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
                minconn=10,  
                maxconn=80, 
                host=os.getenv('PGHOST', 'localhost'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port="5432"
            )
            conn = self.connection_pool.getconn()
            self.connection_pool.putconn(conn)
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
        try:
            filepath = os.path.join(os.path.dirname(__file__), '..', 'db', 'schema.sql')
            with open(filepath, 'r') as f:
                schema = f.read()
                with self.db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(schema)
                    conn.commit()
        except (Exception, Error) as error:
            logger.error("Error while initializing database: %s", error)
            sys.exit(1)
    
    def update_feeds_list(self):
        """Update list of rss feeds, currently only from smallweb project"""
        smallweb_feeds = self.get_smallweb_feeds()

        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT feed_url FROM feeds")
                existing_feeds = {row[0] for row in cursor.fetchall()}        
                new_feeds = [feed.strip() for feed in smallweb_feeds if feed not in existing_feeds]

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
            logger.error("Error updating feeds list: %s", e)

    def get_smallweb_feeds(self) -> set[str]:
        """Get list of rss feeds from smallweb project"""
        feeds_file_url = 'https://raw.githubusercontent.com/kagisearch/smallweb/refs/heads/main/smallweb.txt'
        try:
            response = requests.get(feeds_file_url)
            response.raise_for_status()  
            
            return {line.strip().rstrip('/') for line in response.text.splitlines() if line.strip()}
        
        except Exception as e:
            logger.error("Error downloading feeds file: %s", e)
            return set()
    
    def enqueue_new_feed_entries(self, max_workers: int = MAX_WORKERS, only_due_for_update: bool = False):
        feeds = self.get_all_feeds_from_db(only_due_for_update=only_due_for_update) #True in prod
        #feeds = feeds[6000:6005] # subset of feeds for testing
        
        logger.info("\nProcessing %d feeds with %d worker threads\n", len(feeds), max_workers)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executor.map(self.process_feed, feeds)
            
        logger.info("All feeds processed")
    
    def get_all_feeds_from_db(self, only_due_for_update: bool = False):
        """Get list of all rss feeds from database"""
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                if only_due_for_update:
                    cursor.execute("SELECT feed_url FROM feeds WHERE last_check_date < CURRENT_DATE - INTERVAL '1 day' OR last_check_date IS NULL")
                else:
                    cursor.execute("SELECT feed_url FROM feeds")
                feeds = {row[0] for row in cursor.fetchall()}
                return feeds
        except (Exception, Error) as error:
            logger.error("Error getting all feeds: %s", error)
            return set()

    def process_feed(self, feed):
        """Process a single feed, extract new urls and enqueue them"""
        logger.info("Processing feed: %s", feed)
        try:  
            parsed_feed = fastfeedparser.parse(feed)
            link = parsed_feed.feed.link
            if not link:
                logger.warning("Feed link not found") 
                return # is this necessary?
            #logger.info("Feed link: %s", link)
            logger.info("Found %d entries for feed %s", len(parsed_feed.entries), feed)
        except Exception as e:
            logger.error("Error parsing feed %s: %s", feed, e)
            return

        # Collect candidate URLs first
        candidate_urls = set()
        try:
            for entry in parsed_feed.entries:
                if hasattr(entry, 'link'):
                    entry_link = entry.link.strip().rstrip('/')
                    if not self.is_url_valid(entry_link):
                        logger.debug("Skipping invalid URL: %s", entry_link)
                        continue
                    if not self.is_blog_post_url(entry_link):
                        logger.debug("Skipping non-blog URL: %s", entry_link)
                        continue
                    candidate_urls.add(entry_link)
        except Exception as e:
            logger.error("Error processing feed entries %s: %s", feed, e)
            return
            
        if not candidate_urls:
            logger.info("No valid URLs found for feed %s", feed)
            self.mark_feed_as_checked(feed)
            return

        # Check which URLs already exist in a single batch query
        existing_urls = self.batch_check_urls_exist(candidate_urls)
        new_urls = candidate_urls - existing_urls
        
        logger.info("Found %d new URLs out of %d for feed %s", len(new_urls), len(candidate_urls), feed)
        if new_urls:
            limited_urls = list(new_urls)[:100] # to avoid too large message size
            self.sqs_queue.send_message(limited_urls)
            logger.info("Sent %d new URLs to the queue", len(limited_urls))
        
        self.mark_feed_as_checked(feed)
    
    def batch_check_urls_exist(self, urls: set) -> set:
        """Check which URLs already exist in the database in a single batch query"""
        if not urls:
            return set()
            
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                # Convert set to list for SQL
                url_list = list(urls)
                # Create placeholders for the SQL query
                placeholders = ','.join(['%s'] * len(url_list))
                query = f"SELECT url FROM pages WHERE url IN ({placeholders})"
                cursor.execute(query, url_list)
                existing_urls = {row[0] for row in cursor.fetchall()}
                cursor.close()
                return existing_urls
        except (Exception, Error) as error:
            logger.error("Error batch checking URLs: %s", error)
            return set() # Assume all exist on error to avoid duplicates
    
    def is_blog_post_url(self, url: str) -> bool:
        """Basic check if a url follow known non-blog patterns"""
        non_blog_patterns = [
            r'^.*/(about|links|tags|categories|archive|contact)(/.*)?$',  
            r'^.*/(author|tag|category)/[^/]+(/.*)?$',                    
            r'^.*/(tag|category)(/.*)?$'                                   
        ]
        
        for pattern in non_blog_patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return False
        
        return True
    
    def mark_feed_as_checked(self, feed_url: str):
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE feeds SET last_check_date = CURRENT_DATE WHERE feed_url = %s", (feed_url,))
                conn.commit()
        except (Exception, Error) as error:
            logger.error("Error marking feed as checked: %s", error)

    def scrape(self, max_workers: int = MAX_WORKERS):
        """Scrape all urls from the queue and save them to the database, multithreaded"""
        #self.enqueue_new_feed_entries(only_due_for_update=True)
        logger.info("Starting scrape with %d worker threads", max_workers)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for _ in range(max_workers):
                executor.submit(self.process_message_from_queue)
            
            executor.shutdown(wait=True)
            
        logger.info("Scraping complete - no more messages in queue")
    
    def process_message_from_queue(self):
        """Process messages from the queue until the queue is empty."""
        logger.info("Message processor started")
        
        while True:
            message = self.sqs_queue.receive_message()
            if not message:
                logger.info("No more messages in queue, exiting")
                return  
            
            try:
                self.process_single_message(message)
            except Exception as e:
                logger.error("Error processing message: %s", e)
                sleep(5)
    
    def process_single_message(self, message):
        """Process a single message from the queue.""" 
        try:
            receipt_handle = message['ReceiptHandle']
            body = json.loads(message['Body'])
            urls = body['urls']
            
            #logger.info("Processing message with %d URLs", len(urls))
            
            visibility_timeout = self.calculate_visibility_timeout(len(urls))
            self.sqs_queue.change_message_visibility(receipt_handle, visibility_timeout)
            logger.info("Changed message visibility to %d seconds", visibility_timeout)
            task_timeout = int(visibility_timeout * 0.8)
            
            # Filter URLs for validity first
            valid_urls = [url for url in urls if self.is_url_valid(url) and self.is_blog_post_url(url)]
            
            # Check which URLs already exist in database in one batch query
            existing_urls = self.batch_check_urls_exist(set(valid_urls))
            urls_to_scrape = [url for url in valid_urls if url not in existing_urls]
            
            # Further filter by robots.txt
            urls_to_scrape = [url for url in urls_to_scrape if self.robots_allows_scraping(url)]

            if len(urls_to_scrape) > 30: # remove this limit later
                urls_to_scrape = urls_to_scrape[:30]
            
            logger.info("Processing %d new URLs sequentially from %d original URLs", 
                       len(urls_to_scrape), len(urls))
            
            error_count = 0
            for url in urls_to_scrape:
                if error_count > 3:
                    logger.error("Too many errors for %s, skipping remaining URLs", get_base_url(url))
                    break
                try:
                    self.scrape_url(url) #add check_url(url)? 
                    if url != urls_to_scrape[-1]:
                        sleep(5) # basic rate limiting
                except Exception as e:
                    logger.error("Error scraping URL %s: %s", url, e)
                    error_count += 1

            self.sqs_queue.delete_message(receipt_handle)
            logger.info("Deleted message after processing all URLs")
            
        except Exception as e:
            logger.error("Error processing message: %s", e)

    def calculate_visibility_timeout(self, num_urls: int) -> int:
        """Calculate visibility timeout for a message based on the number of urls"""
        timout = 6
        scrape_delay = 6
        processing_delay = 2
        safety_margin = 2.5
        return math.ceil(safety_margin * (num_urls * (timout + scrape_delay + processing_delay)))
    
    def url_exists_in_db(self, url: str) -> bool:
        """Check if a url already exists in the database"""
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT EXISTS(SELECT 1 FROM pages WHERE url = %s)", (url,))
                exists = cursor.fetchone()[0]
                cursor.close()
                return exists
        except (Exception, Error) as error:
            logger.error("Error checking URL existence: %s", error)
            return True
    
    def is_url_valid(self, url: str) -> bool:
        """Check if a url is valid"""
        return is_valid_url(url)

    def robots_allows_scraping(self, url: str) -> bool:
        """Check if robots.txt allows scraping for a given domain and url"""
        base_url = get_base_url(url)
        
        # Check in-memory cache first
        if base_url in self.robots_cache:
            return self.robots_cache[base_url].can_fetch("*", url)
        
        # Fetch robots.txt directly
        rp = RobotFileParser()
        robots_url = f"{base_url}/robots.txt"
        try:
            rp.set_url(robots_url)
            rp.read()
            self.robots_cache[base_url] = rp
            return rp.can_fetch("*", url)
        except Exception as e:
            logger.warning("Error fetching robots.txt for %s (allowing scrape): %s", base_url, e)
            # Cache a permissive robots parser for failed fetches to avoid repeated requests
            permissive_rp = RobotFileParser()
            permissive_rp.set_url(robots_url)
            # Empty robots.txt allows everything
            permissive_rp.parse([])
            self.robots_cache[base_url] = permissive_rp
            return True

    def scrape_url(self, url: str):
        """Scrape a single url, save it to the database if it's a blog post"""
        logger.info("Scraping URL: %s", url)

        try:
            downloaded_content = trafilatura.fetch_url(url)
        except Exception as e:
            logger.error("Error downloading content for %s: %s", url, e)
            return
        if not downloaded_content:
            logger.warning("Failed to download content for %s", url)
            return
        
        if not trafilatura.readability_lxml.is_probably_readerable(downloaded_content):
            logger.warning("Downloaded content for %s is not readerable", url)
            return
        
        extracted = trafilatura.extract(downloaded_content, output_format='json', with_metadata=True)
        if not extracted:
            logger.warning("Failed to extract text for %s", url)
            return
        
        extracted_dict = json.loads(extracted)
        if len(extracted_dict['raw_text'].split()) < 100:
            logger.warning("Extracted text for %s is too short", url)
            return
        
        page = {
            'title': extracted_dict['title'],
            'url': url,
            'fingerprint': extracted_dict['fingerprint'],
            'date': extracted_dict['date'],
            'text': extracted_dict['raw_text']
        }
        self.save_page(page)
        logger.info("Successfully processed and saved page: %s", url)
    
    def save_page(self, page: dict):
        """Save a page to the database"""
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO pages (title, url, fingerprint, date, text) VALUES (%s, %s, %s, %s, %s)", 
                              (page['title'], page['url'], page['fingerprint'], page['date'], page['text']))
                conn.commit()
        except (Exception, Error) as error:
            logger.error("Error saving page: %s", error)

    def tmp(self):
        pass

    @contextmanager
    def db_connection(self):
        """Context manager for database connections."""
        conn = None
        try:
            conn = self.get_connection()
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if conn:
                self.release_connection(conn)


if __name__ == "__main__":
    scraper = Scraper()
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "update_feeds":
            logger.info("UPDATING FEEDS LIST")
            scraper.update_feeds_list()
        elif command == "enqueue":
            logger.info("ENQUEUE")
            scraper.enqueue_new_feed_entries()
        elif command == "scrape":
            logger.info("SCRAPING")
            scraper.scrape()
        elif command == "tmp":
            scraper.tmp()
        else:
            logger.error("Unknown command: %s", command)
            logger.info("Available commands: update_feeds, enqueue, scrape, tmp")
    else:
        logger.info("Available commands: update_feeds, enqueue, scrape, tmp")


