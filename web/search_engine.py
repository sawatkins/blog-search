from dotenv import load_dotenv
import re
import os
import sys
import psycopg2
from psycopg2 import Error

class SearchEngine:
    def __init__(self, db_name='pages.db'):
        load_dotenv()
        self.connection: psycopg2.extensions.connection
        self.connect()
        self.size = self.get_db_size()

    def connect(self):
        try:
            self.connection = psycopg2.connect(
                host=os.getenv('PGHOST'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port="5432"
            )
        except (Exception, Error) as error:
            print("Error while connecting to PostgreSQL:", error)
            sys.exit(1)

    def ensure_connection(self):
        try:
            with self.connection.cursor() as cursor:
                cursor.execute('SELECT 1')
        except (Exception, Error):
            self.connect()

    def get_db_size(self):
        self.ensure_connection()
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM pages_old')
            result = cursor.fetchone()
            return result[0] if result else 0
    
    def clean_text(self, text):
        if text is None:
            return ''
        return re.sub(r'\s+', ' ', text.replace('\n', ' '), flags=re.MULTILINE).strip().lower()

    def search(self, query):
        self.ensure_connection()
        query_words = self.clean_text(query)
        if not query_words:
            return []
        
        with self.connection.cursor() as cursor:
            sql = """
                SELECT title, url, date, text,
                    ts_rank_cd(page_tsv, phraseto_tsquery('english', %s)) as rank
                FROM pages_old
                WHERE page_tsv @@ phraseto_tsquery('english', %s)
                ORDER BY rank DESC
                LIMIT 100
            """
            cursor.execute(sql, (query_words, query_words))
            results = cursor.fetchall()
            
            return [
                {
                    'title': row[0],
                    'url': row[1].strip("/"),
                    'date': row[2],
                    'text': row[3]
                }
                for row in results
            ]

if __name__ == "__main__":
    engine = SearchEngine()
    
    while True:
        query = input("Enter your search query (or 'quit' to exit): ")
        if query.lower() == 'quit':
            break
        
        results = engine.search(query)
        print(f"\nFound {len(results)} results:")
        for post in results[:5]:  # Display top 5 results
            print(f"Title: {post['title']}")
            print(f"URL: {post['url']}")
            print(f"Date: {post['date']}")
            print(f"Text: {post['text'][:100]}...")
            print()