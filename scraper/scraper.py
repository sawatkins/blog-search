import sys
import os
from dotenv import load_dotenv
from psycopg2 import Error
from psycopg2 import pool
import fastfeedparser
import requests
from sqs_queue import SQSQueue


class Scraper:
    def __init__(self):
        load_dotenv()
        self.connection_pool: pool.ThreadedConnectionPool 
        self.init_pool()
        self.init_db()        
        self.sqs_queue = SQSQueue()

    def init_pool(self):
        try:
            self.connection_pool = pool.ThreadedConnectionPool(
                minconn=1,  
                maxconn=3, 
                host=os.getenv('PGHOST'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port="5432"
            )
        except (Exception, Error) as error:
            print("Error while creating connection pool:", error)
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
            print("Error while initializing database:", error)
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

            print(f"found {len(existing_feeds)} existing feeds")
            print(f"found {len(new_feeds)} new feeds")
            
            if new_feeds:
                insert_query = "INSERT INTO feeds (feed_url) VALUES (%s) ON CONFLICT DO NOTHING"
                cursor.executemany(insert_query, [(feed,) for feed in new_feeds])
                conn.commit()
                print(f"Added {len(new_feeds)} new feeds to the database")
            else:
                print("No new feeds to add")
            
            cursor.close()
        except Exception as e:
            conn.rollback()
            print(f"Error updating feeds list: {e}")
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
            print(f"Error downloading feeds file: {e}")
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
            print(f"Error getting all feeds: {error}")
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
            print(f"Error marking feed as checked: {error}")
        finally:
            self.release_connection(conn)

    def scrape(self):
        #self.enqueue_new_feed_entries()
        self.process_queue()
    
    def url_exists_in_db(self, url: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pages WHERE url = %s)", (url,))
            exists = cursor.fetchone()[0]
            cursor.close()
            return exists
        except (Exception, Error) as error:
            print(f"Error checking URL existence: {error}")
            return True
        finally:
            self.release_connection(conn)
    
    def enqueue_new_feed_entries(self):
        # TODO: make this multi-threaded
        # TODO: add logic to handle errors
        # TODO: update feeds table with last check date
        feeds = self.get_all_feeds(only_due_for_update=False)
        feeds = feeds[6000:6005] # subset of feeds for testing
        for feed in feeds:
            new_urls = []
            parsed_feed = fastfeedparser.parse(feed)
            link = parsed_feed.feed.link
            print(link)
            print(len(parsed_feed.entries), "entries")
            
            for entry in parsed_feed.entries:
                if hasattr(entry, 'link'):
                    entry_link = entry.link.strip().rstrip('/')
                    # TODO: remove known url patterns that are not blog posts
                    exists = self.url_exists_in_db(entry_link)
                    if not exists:
                        new_urls.append(entry_link)
                    print(f"URL {entry_link}: {'exists' if exists else 'new'}")

            print("new urls:", len(new_urls))
            if new_urls:
                self.sqs_queue.send_message(new_urls)
                print(f"sent {len(new_urls)} new urls to the queue") 
            
            self.mark_feed_as_checked(feed)
    
    def process_queue(self):
        # TODO: later, make this multi-threaded
        message = self.sqs_queue.receive_message()
        if message:
            self.process_message(message)
    
    def process_message(self, message: dict):
        # TODO: Add logic to download and process the page
        # For now, just acknowledge processing is complete
        url = message[0]['Body']
        print(f"Processing URL: {url}")
            
        receipt_handle = message[0]['ReceiptHandle']
        self.sqs_queue.delete_message(receipt_handle)
        print(f"Processed and deleted message for URL: {url}")

    def tmp(self):
        feeds = self.get_all_feeds(only_due_for_update=False)
        print(len(feeds))



if __name__ == "__main__":
    scraper = Scraper()
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "update_feeds":
            scraper.update_feeds_list()
        elif command == "scrape":
            scraper.scrape()
        elif command == "tmp":
            scraper.tmp()
        else:
            print(f"unknown command: {command}")
            print("available commands: update_feeds, scrape, tmp")
    else:
        print("available commands: update_feeds, scrape, tmp")


