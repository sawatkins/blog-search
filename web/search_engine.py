from collections import defaultdict
from dotenv import load_dotenv
import sqlite3
import re
import os
import math
import psycopg2
from psycopg2 import Error

class SearchEngine:
    def __init__(self, db_name='pages.db'):
        load_dotenv()
        try:
            self.connection = psycopg2.connect(
                host=os.getenv('PGHOST'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                port="5432"
            )
            self.size = self.get_db_size()
        except (Exception, Error) as error:
            print("Error while connecting to PostgreSQL:", error)
        
        
        # self.db_name = db_name
        # self.posts = self.load_posts_from_db()
        # self.posts_size = len(self.posts)
        # self.index = defaultdict(dict)
        # self.doc_lengths = {}
        # self.avg_doc_length = 0
        # self.total_docs = len(self.posts)
        # self.create_index()

    # def load_posts_from_db(self):
    #     conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), '../data/', self.db_name))
    #     cursor = conn.cursor()
    #     cursor.execute('SELECT title, url, date, text FROM pages')
    #     posts = [
    #         {'title': row[0], 'url': row[1], 'date': row[2], 'text': row[3]}
    #         for row in cursor.fetchall()
    #     ]
    #     conn.close()
    #     return posts
    
    def get_db_size(self):
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM pages_old')
            result = cursor.fetchone()
            return result[0] if result else 0
    
    def clean_text(self, text):
        if text is None:
            return ''
        return re.sub(r'\s+', ' ', text.replace('\n', ' '), flags=re.MULTILINE).strip().lower()

    # def create_index(self):
    #     total_length = 0
    #     for post_id, post in enumerate(self.posts):
    #         title = self.clean_text(post['title'])
    #         text = self.clean_text(post['text'])
    #         words = title.split() + text.split()
    #         doc_length = len(words)
    #         self.doc_lengths[post_id] = doc_length
    #         total_length += doc_length

    #         word_freq = defaultdict(int)
    #         for word in words:
    #             word_freq[word] += 1

    #         for word, freq in word_freq.items():
    #             self.index[word][post_id] = freq

    #     self.avg_doc_length = total_length / self.total_docs #does this work?
    #     print(f"Indexed {self.total_docs} pages")

    # def bm25_score(self, query_words, post_id):
    #     k1 = 1.5
    #     b = 0.75
    #     score = 0
    #     for word in query_words:
    #         if word not in self.index or post_id not in self.index[word]:
    #             continue
    #         tf = self.index[word][post_id]
    #         df = len(self.index[word])
    #         idf = math.log((self.total_docs - df + 0.5) / (df + 0.5) + 1)
    #         score += idf * ((tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (self.doc_lengths[post_id] / self.avg_doc_length))))
    #     return score

    def search(self, query):
        query_words = self.clean_text(query)
        if not query_words:
            return []
        
        with self.connection.cursor() as cursor:
            sql = """
                SELECT title, url, date, text,
                    ts_rank(to_tsvector('english', title || ' ' || text), 
                           phraseto_tsquery('english', %s)) as rank
                FROM pages_old 
                WHERE to_tsvector('english', title || ' ' || text) @@ 
                      phraseto_tsquery('english', %s)
                ORDER BY rank DESC 
                LIMIT 10
            """
            cursor.execute(sql, (query_words, query_words))
            results = cursor.fetchall()
            
            return [
                {
                    'title': row[0],
                    'url': row[1],
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