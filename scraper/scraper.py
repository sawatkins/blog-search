import sys
import os
from dotenv import load_dotenv
from psycopg2 import Error
from psycopg2 import pool
import fastfeedparser
import requests
from sqs_queue import SQSQueue


class Scraper:
    def __init__(self) -> None:
        load_dotenv()
        self.connection_pool = None
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
            self.connection_pool = None

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
            return False
        finally:
            self.release_connection(conn)
    
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
    scraper = Scraper()
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "update_feeds":
            scraper.update_feeds_list()
        else:
            print(f"unknown command: {command}")
            print("available commands: update_feeds")
    else:
        print("available commands: update_feeds")


