from dotenv import load_dotenv
import re
import os
import sys
from psycopg2 import Error
from psycopg2 import pool

class SearchEngine:
    def __init__(self, db_name='pages.db'):
        load_dotenv()
        self.connection_pool = None
        self.init_pool()
        self.init_db()
        self.size = self.get_db_size()

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

    def get_db_size(self):
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM pages_old')
                result = cursor.fetchone()
                return result[0] if result else 0
        finally:
            self.release_connection(conn)

    def clean_text(self, text):
        if text is None:
            return ''
        return re.sub(r'\s+', ' ', text.replace('\n', ' '), flags=re.MULTILINE).strip().lower()

    def search(self, query):
        conn = self.get_connection()
        try:
            query_words = self.clean_text(query)
            if not query_words:
                return []
            
            with conn.cursor() as cursor:
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
        finally:
            self.release_connection(conn)

if __name__ == "__main__":
    engine = SearchEngine()
    
    try:
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
    finally:
        engine.close_pool()