import sys
import os
from dotenv import load_dotenv
import psycopg2
from sqs_queue import SQSQueue
import fastfeedparser
import requests

class Scraper:
    def __init__(self, db_name='') -> None:
        self.connection: psycopg2.extensions.connection
        self.sqs_queue = SQSQueue()
        self.connect_db()
        self.init_db()
        self.smallweb_feeds = self.get_smallweb_feeds()

    def connect_db(self) -> None:
        try:
            self.connection = psycopg2.connect(
                host=os.getenv('PGHOST'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port="5432"
            )
        except (Exception, psycopg2.Error) as error:
            print("Error while connecting to PostgreSQL:", error)
            sys.exit(1)

    def init_db(self) -> None:
        try:
            cursor = self.connection.cursor()
            # what is even the point of the feeds table???
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS feeds ( 
                    feed_url TEXT PRIMARY KEY,
                    domain TEXT,
                    last_check_date DATE,
                    is_active BOOLEAN DEFAULT true,
                    date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS pages (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    feed_url TEXT REFERENCES feeds(feed_url),
                    fingerprint TEXT UNIQUE,
                    date DATE,
                    text TEXT,
                    scraped_on_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            self.connection.commit()
            cursor.close()
        except (Exception, psycopg2.Error) as error:
            print("Error while initializing database:", error)
            sys.exit(1)
    
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
        
    def url_exists_in_db(self, url: str) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pages WHERE url = %s)", (url,))
            exists = cursor.fetchone()[0]
            cursor.close()
            return exists
        except (Exception, psycopg2.Error) as error:
            print(f"Error checking URL existence: {error}")
            return False
    
    def parse_feed_links(self):
        for feed in self.smallweb_feeds[6000:6005]:
            new_links = []
            parsed_feed = fastfeedparser.parse(feed)
            link = parsed_feed.feed.link
            print(link)
            print(len(parsed_feed.entries), "entries")
            
            for entry in parsed_feed.entries:
                if hasattr(entry, 'link'):
                    exists = self.url_exists_in_db(entry.link)
                    if not exists:
                        new_links.append(entry.link)
                    print(f"URL {entry.link}: {'exists' if exists else 'new'}")

            print("new links:", len(new_links))


    
    def normalize_feed_url(self, url: str):
        #url = url.rstrip('/')
        pass


if __name__ == "__main__":
    print("starting scraper...")
    load_dotenv()
    scraper = Scraper()
    print("connected to db and sqs")

    #scraper.get_new_smallweb_urls()
    #feed = scraper.get_smallweb_feeds()
    print(len(scraper.smallweb_feeds))
    scraper.parse_feed_links()
