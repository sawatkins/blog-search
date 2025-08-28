import json
import logging
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
from meilisearch import Client as MeiliSearch

from sqs_queue import SQSQueue

MAX_WORKERS = 19

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

# Suppress trafilatura's verbose logging
logging.getLogger('trafilatura').setLevel(logging.WARNING)

class Scraper:
    def __init__(self):
        load_dotenv()
        self.connection_pool: pool.ThreadedConnectionPool 
        self.init_pool()
        self.init_db()        
        self.sqs_queue = SQSQueue()
        self.meilisearch_client = None
        self.init_meilisearch()

    def init_pool(self):
        try:
            self.connection_pool = pool.ThreadedConnectionPool(
                minconn=5,  
                maxconn=20, 
                host=os.getenv('PGHOST'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port=os.getenv('PGPORT', 5432),
                sslmode=os.getenv('PGSSLMODE', 'prefer'),
                channel_binding=os.getenv('PGCHANNELBINDING', 'prefer')
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

    def init_meilisearch(self):
        """Initialize Meilisearch client with error handling"""
        try:
            self.meilisearch_client = MeiliSearch(
                url=os.getenv('MEILISEARCH_URL'),
                api_key=os.getenv('MEILISEARCH_API_KEY')
            )
            # Test connection
            self.meilisearch_client.health()
            logger.info("Connected to Meilisearch at %s", os.getenv('MEILISEARCH_URL'))
        except Exception as e:
            logger.warning("Could not connect to Meilisearch (will skip indexing): %s", e)
            self.meilisearch_client = None
    
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
    
    def enqueue_new_feed_entries(self, max_workers: int = MAX_WORKERS):
        # Clear any existing messages from previous runs
        logger.info("Clearing SQS queue before enqueuing new feeds")
        self.sqs_queue.purge_queue()
        
        feeds = self.get_smallweb_feeds()
        #feeds = list(feeds)[6000:6005] # subset of feeds for testing
        
        logger.info("\nProcessing %d feeds with %d worker threads\n", len(feeds), max_workers)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executor.map(self.process_feed, feeds)
            
        logger.info("All feeds processed")
    

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
                    entry_link = clean_url(entry.link)  # Normalize URL
                    if not entry_link or not self.is_url_valid(entry_link):
                        logger.debug("Skipping invalid URL: %s", entry.link)
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
            # No longer tracking feed check dates since we use smallweb.txt directly
            return

        # Check which URLs already exist in a single batch query
        existing_urls = self.batch_check_urls_exist(candidate_urls)
        new_urls = candidate_urls - existing_urls
        
        logger.info("Found %d new URLs out of %d for feed %s", len(new_urls), len(candidate_urls), feed)
        if new_urls:
            limited_urls = list(new_urls)[:30] # to avoid too large message size
            self.sqs_queue.send_message(limited_urls)
            logger.info("Sent %d new URLs to the queue", len(limited_urls))
        
    
    def batch_check_urls_exist(self, urls: set) -> set:
        """Check which URLs already exist in the database (checking both url and original_url columns)"""
        if not urls:
            return set()
            
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                url_list = list(urls)
                
                # Get rows where candidates appear in either column
                cursor.execute("""
                    SELECT url, original_url 
                    FROM pages 
                    WHERE url = ANY(%s) OR original_url = ANY(%s)
                """, [url_list, url_list])
                
                results = cursor.fetchall()
                
                # Check which candidates we found
                existing = set()
                for final_url, original_url in results:
                    if final_url in urls:
                        existing.add(final_url)
                    if original_url and original_url in urls:
                        existing.add(original_url)
                
                cursor.close()
                return existing
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
            
            if len(urls) > 30: 
               urls = urls[:30]
            
            #logger.info("Processing message with %d URLs", len(urls))
            
            visibility_timeout = self.calculate_visibility_timeout(len(urls))
            self.sqs_queue.change_message_visibility(receipt_handle, visibility_timeout)
            #logger.info("Changed message visibility to %d seconds", visibility_timeout)
            task_timeout = int(visibility_timeout * 0.8)
            
            # Normalize and filter URLs for validity first
            valid_urls = []
            for url in urls:
                normalized_url = clean_url(url)
                if normalized_url and self.is_url_valid(normalized_url) and self.is_blog_post_url(normalized_url):
                    valid_urls.append(normalized_url)
            
            # Check which URLs already exist in database in one batch query
            existing_urls = self.batch_check_urls_exist(set(valid_urls))
            urls_to_scrape = [url for url in valid_urls if url not in existing_urls]
            
            # Check robots.txt once for this domain (since all URLs in message are same domain)
            robot_parser = None
            if urls_to_scrape:
                domain_url = get_base_url(urls_to_scrape[0])
                robot_parser = self.check_robots_for_domain(domain_url)
                # Filter URLs by robots.txt rules
                urls_to_scrape = [url for url in urls_to_scrape if self.robots_allows_scraping(robot_parser, url)]

            
            logger.info("Processing %d new URLs sequentially from %d original URLs", 
                       len(urls_to_scrape), len(urls))
            
            error_count = 0
            consecutive_errors = 0
            sleep_time = 4
            
            for url in urls_to_scrape:
                if consecutive_errors >= 2:
                    logger.error("Too many consecutive errors for %s, backing off", get_base_url(url))
                    sleep(sleep_time)
                
                if error_count >= 4:
                    logger.error("Too many errors for %s, skipping and deleting message for", get_base_url(url))
                    self.sqs_queue.delete_message(receipt_handle)
                    return
                
                try:
                    self.scrape_url(url)
                    consecutive_errors = 0  
                    if url != urls_to_scrape[-1]:
                        sleep(sleep_time)
                except Exception as e:
                    error_count += 1
                    consecutive_errors += 1
                    logger.error("Error scraping %s (consecutive: %d): %s", url, consecutive_errors, e)
                    sleep(sleep_time)  

            self.sqs_queue.delete_message(receipt_handle)
            logger.info("Deleted message after processing all URLs")
            
        except Exception as e:
            logger.error("Error processing message: %s", e)

    def calculate_visibility_timeout(self, num_urls: int) -> int:
        """Calculate visibility timeout for a message based on the number of urls"""
        # 10s timeout + 5s sleep + 5s processing buffer = 20s per URL, plus 60s safety margin
        return max(300, num_urls * 20 + 60)  # minimum 5 minutes, max based on URL count
    
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

    def check_robots_for_domain(self, domain_url: str) -> RobotFileParser:
        """Check robots.txt for a domain once per message processing"""
        rp = RobotFileParser()
        robots_url = f"{domain_url}/robots.txt"
        try:
            rp.set_url(robots_url)
            rp.read()
            logger.info("Loaded robots.txt for %s", domain_url)
            return rp
        except Exception as e:
            logger.warning("Error fetching robots.txt for %s (allowing all): %s", domain_url, e)
            # Create permissive robots parser for failed fetches
            permissive_rp = RobotFileParser()
            permissive_rp.set_url(robots_url)
            # Empty robots.txt allows everything
            permissive_rp.parse([])
            return permissive_rp
    
    def robots_allows_scraping(self, robot_parser: RobotFileParser, url: str) -> bool:
        """Check if robots.txt allows scraping for a specific URL"""
        return robot_parser.can_fetch("*", url)

    def scrape_url(self, url: str):
        """Scrape a single url, save it to the database if it's a blog post"""
        logger.info("Scraping URL: %s", url)

        try:
            config = trafilatura.settings.use_config()
            try:
                config.set('DEFAULT', 'USER_AGENT', 'BlogSearchBot/1.0 (+https://blogsearch.io/bot)')
                config.set('DEFAULT', 'DOWNLOAD_TIMEOUT', '10')
            except Exception:
                pass

            downloaded_content = trafilatura.fetch_url(url, config=config)
        except Exception as e:
            logger.error("Error downloading content for %s: %s", url, e)
            raise e
        
        if not downloaded_content:
            logger.warning("Failed to download content for %s", url)
            raise Exception("Failed to download content for %s", url)
        
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
        
        # Use the final URL from trafilatura if available (handles redirects)
        final_url = extracted_dict.get('url', url)
        if final_url != url:
            logger.info("URL redirected from %s to %s", url, final_url)
        
        page = {
            'title': extracted_dict['title'],
            'url': clean_url(final_url),  # Normalize the final URL
            'original_url': clean_url(url),  # Store the original URL from RSS feed
            'fingerprint': extracted_dict['fingerprint'],
            'date': extracted_dict['date'],
            'text': extracted_dict['raw_text']
        }
        self.save_page(page)
        logger.info("Successfully processed and saved page: %s", url)
    
    def save_page(self, page: dict):
        """Save a page to the database and index in Meilisearch"""
        db_saved = False
        try:
            with self.db_connection() as conn:
                cursor = conn.cursor()
                # Use ON CONFLICT to handle duplicate URLs gracefully
                cursor.execute("""
                    INSERT INTO pages (title, url, original_url, fingerprint, date, text) 
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO UPDATE
                        SET title = EXCLUDED.title,
                            original_url = COALESCE(pages.original_url, EXCLUDED.original_url),
                            fingerprint = EXCLUDED.fingerprint,
                            date = EXCLUDED.date,
                            text = EXCLUDED.text,
                            scraped_on_date = CURRENT_TIMESTAMP
                    RETURNING id
                """, (page['title'], page['url'], page['original_url'], page['fingerprint'], page['date'], page['text']))
                
                result = cursor.fetchone()
                if result:
                    page_id = result[0]
                    db_saved = True
                    logger.info("Successfully saved/updated page: %s", page['url'])
                conn.commit()
                
                # Add to Meilisearch if DB save was successful and client is available
                if db_saved and self.meilisearch_client:
                    self.index_page_in_meilisearch(page, page_id)
                    
        except Error as error:
            # Check if it's a fingerprint duplicate error
            if 'fingerprint' in str(error) and 'duplicate' in str(error).lower():
                logger.info("Page with identical fingerprint already exists; skipping URL: %s", page['url'])
            else:
                logger.error("Error saving page %s: %s", page['url'], error)

    def index_page_in_meilisearch(self, page: dict, page_id: int):
        """Index a page in Meilisearch with error handling"""
        if not self.meilisearch_client:
            return
            
        try:
            document = {
                'id': str(page_id),  
                'title': page['title'] or '',
                'url': page['url'] or '',
                'date': page.get('date') or None,
                'text': page['text'] or ''
            }
            
            # Add document to Meilisearch
            index = self.meilisearch_client.index('pages')
            task = index.add_documents([document])
            logger.debug("Added page to Meilisearch search index: %s (task: %s)", page['url'], task.task_uid)
            
        except Exception as e:
            logger.warning("Failed to index page in Meilisearch %s: %s", page['url'], e)
            # Continue execution - don't fail the scrape if Meilisearch fails

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
        if command == "enqueue":
            logger.info("ENQUEUE")
            scraper.enqueue_new_feed_entries()
        elif command == "process":
            logger.info("PROCESSING")
            scraper.scrape()
        elif command == "run":
            logger.info("RUN (ENQUEUE + PROCESS)")
            scraper.enqueue_new_feed_entries()
            logger.info("Enqueue complete, starting processing...")
            scraper.scrape()
        elif command == "tmp":
            scraper.tmp()
        else:
            logger.error("Unknown command: %s", command)
            logger.info("Available commands: enqueue, process, run, tmp")
    else:
        logger.info("Available commands: enqueue, process, run, tmp")


