import sys
import os
from dotenv import load_dotenv
import psycopg2

class Scraper:
    def __init__(self, db_name='pages') -> None:
        self.connection: psycopg2.extensions.connection
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



if __name__ == "__main__":
    print("starting scraper...")
    load_dotenv()
    scraper = Scraper()
    print("connected to db")
