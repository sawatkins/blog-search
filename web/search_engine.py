from dotenv import load_dotenv
import re
import os
import sys
from psycopg2 import Error
from psycopg2 import pool
from meilisearch import Client as MeiliSearch

class SearchEngine:
    def __init__(self, use_meilisearch: bool = True):
        load_dotenv()
        self.connection_pool = None
        self.init_pool()
        self.init_db()
        self.size = self.get_db_size()
        self.meilisearch_client = None
        if use_meilisearch:
            self.init_meilisearch()

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

    def get_db_size(self):
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM pages')
                result = cursor.fetchone()
                return result[0] if result else 0
        finally:
            self.release_connection(conn)

    def clean_text(self, text):
        if text is None:
            return ''
        return re.sub(r'\s+', ' ', text.replace('\n', ' '), flags=re.MULTILINE).strip().lower()

    def search(self, query) -> list[dict]:
        conn = self.get_connection()
        try:
            query_words = self.clean_text(query)
            if not query_words:
                return []
            
            with conn.cursor() as cursor:
                sql = """
                    WITH search_query AS (
                    SELECT websearch_to_tsquery('english', %s) as q
                    ),

                    hit_ids AS (
                    SELECT pages.id
                    FROM pages, search_query
                    WHERE pages.page_tsv @@ search_query.q
                    LIMIT 6000
                    ),

                    ranked AS (
                    SELECT pages.id, ts_rank_cd(pages.page_tsv, search_query.q) AS score
                    FROM pages
                    JOIN hit_ids ON hit_ids.id = pages.id
                    JOIN search_query ON TRUE 
                    ORDER BY score DESC
                    LIMIT 200
                    )

                    SELECT pages.title,
                        pages.url,
                        pages.date,
                        LEFT(pages.text, 300) AS text,
                        ranked.score
                    FROM ranked
                    JOIN pages on pages.id = ranked.id
                    ORDER BY ranked.score DESC
                    LIMIT 24;
                """
                cursor.execute(sql, (query_words,))
                results = cursor.fetchall()
                
                return [
                    {
                        'title': row[0],
                        'url': row[1].rstrip("/"),
                        'date': row[2],
                        'text': row[3]
                    }
                    for row in results
                ]
        finally:
            self.release_connection(conn)
    
    def init_meilisearch(self):
        self.meilisearch_client = MeiliSearch(
            url=os.getenv('MEILISEARCH_URL'),
            api_key=os.getenv('MEILISEARCH_API_KEY')
        )
    
    def search_meilisearch(self, query: str) -> list[dict]:
        if self.meilisearch_client is None:
            self.init_meilisearch()
        query_words = self.clean_text(query)
        if not query_words:
            return []
        results = self.meilisearch_client.index('pages').search(query_words, {'limit': 24})
        return {
            'results': [
                {
                    'title': result['title'] if 'title' in result else '',
                    'url': result['url'],
                    'date': result['date'] if 'date' in result else '',
                    'text': ' '.join(result['text'].split(' ')[:300]) if 'text' in result else ''
                }
                for result in results['hits']
            ],
            'results_size': results.get('estimatedTotalHits', 0),
            'search_time': results.get('processingTimeMs', 0)
        }
    
    def get_latest_posts(self) -> list[dict]:
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT title, url, date, text FROM pages WHERE date IS NOT NULL ORDER BY date DESC LIMIT 24")
                return [
                    {
                        'title': row[0],
                        'url': row[1].rstrip("/"),
                        'date': row[2],
                        'text': row[3]
                    }
                    for row in cursor.fetchall()
                ]
        finally:
            self.release_connection(conn)

    def get_random_post(self) -> dict:
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT title, url, date, text FROM pages ORDER BY RANDOM() LIMIT 1")
                row = cursor.fetchone()
                return {
                    'title': row[0],
                    'url': row[1].rstrip("/"),
                    'date': row[2],
                    'text': row[3]
                } if row else None
        finally:
            self.release_connection(conn)

    def log_query(self, query: str, ip_address: str, user_agent: str) -> None:
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO query_logs (query, ip_address, user_agent) VALUES (%s, %s, %s)",
                    (query, ip_address, user_agent)
                )
                conn.commit()
        except Exception as e:
            print(f"Error logging query: {e}")
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
            for post in results[:24]:  
                print(f"Title: {post['title']}")
                print(f"URL: {post['url']}")
                print(f"Date: {post['date']}")
                print(f"Text: {post['text'][:100]}...")
                print()
    finally:
        engine.close_pool()