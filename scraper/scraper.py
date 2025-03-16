import sys
import os
from dotenv import load_dotenv
import psycopg2
from sqs_queue import SQSQueue
import fastfeedparser

class Scraper:
    def __init__(self, db_name='') -> None:
        self.connection: psycopg2.extensions.connection
        self.sqs_queue = SQSQueue()
        self.connect_db()
        self.init_db()

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
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS domains (
                    domain TEXT PRIMARY KEY,
                    last_feed_check DATE,
                    date_added TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS pages (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    domain TEXT REFERENCES domains(domain),
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
    
    def get_new_smallweb_urls(self) -> list[str]:
        smallweb_feed_url = "https://kagi.com/api/v1/smallweb/feed?limit=100"
        feed = fastfeedparser.parse(smallweb_feed_url)
        prev_smallweb_urls = self.get_prev_smallweb_urls()
        new_urls = []

        urls = [entry.link for entry in feed.entries]
        print(len(urls))

        new_urls = [url for url in urls if url not in prev_smallweb_urls]
        print(f"Found {len(new_urls)} new URLs")

        self.save_prev_smallweb_urls(urls)
        return new_urls

    def get_prev_smallweb_urls(self) -> list[str]:
        filename = "prev_smallweb.txt"
        urls = []
        
        try:
            if os.path.exists(filename):
                with open(filename, 'r') as file:
                    urls = [line.strip() for line in file]
        except Exception as e:
            print(f"Error reading {filename}: {e}")
            
        return urls

    def save_prev_smallweb_urls(self, urls: list[str]) -> None:
        filename = "prev_smallweb.txt"
        
        try:
            with open(filename, 'w') as file:
                for url in urls:
                    file.write(f"{url.strip()}\n")
        except Exception as e:
            print(f"Error writing to {filename}: {e}")


if __name__ == "__main__":
    print("starting scraper...")
    load_dotenv()
    scraper = Scraper()
    print("connected to db and sqs")

    scraper.get_new_smallweb_urls()
