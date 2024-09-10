from collections import defaultdict
from main import load_posts_from_json
import sqlite3
import re
import math

class SearchEngine:
    def __init__(self, use_db=False, db_name='posts.db'):
        self.use_db = use_db
        self.db_name = db_name
        self.posts = self.load_posts()
        self.index = defaultdict(dict)
        self.doc_lengths = {}
        self.avg_doc_length = 0
        self.total_docs = len(self.posts)
        self.create_index()

    def load_posts(self):
        if self.use_db:
            return self.load_posts_from_db()
        else:
            return load_posts_from_json()

    def load_posts_from_db(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT title, url, date, content FROM posts')
        posts = [
            {'title': row[0], 'url': row[1], 'date': row[2], 'content': row[3]}
            for row in cursor.fetchall()
        ]
        conn.close()
        return posts

    def clean_text(self, text):
        # Remove newlines and extra whitespace
        return re.sub(r'\s+', ' ', text.replace('\n', ' '), flags=re.MULTILINE).strip().lower()

    def create_index(self):
        total_length = 0
        for post_id, post in enumerate(self.posts):
            title = self.clean_text(post['title'])
            content = self.clean_text(post['content'])
            words = title.split() + content.split()
            doc_length = len(words)
            self.doc_lengths[post_id] = doc_length
            total_length += doc_length

            word_freq = defaultdict(int)
            for word in words:
                word_freq[word] += 1

            for word, freq in word_freq.items():
                self.index[word][post_id] = freq

        self.avg_doc_length = total_length / self.total_docs #does this work?
        print(f"Indexed {self.total_docs} posts")

    def bm25_score(self, query_words, post_id):
        k1 = 1.5
        b = 0.75
        score = 0
        for word in query_words:
            if word not in self.index or post_id not in self.index[word]:
                continue
            tf = self.index[word][post_id]
            df = len(self.index[word])
            idf = math.log((self.total_docs - df + 0.5) / (df + 0.5) + 1)
            score += idf * ((tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (self.doc_lengths[post_id] / self.avg_doc_length))))
        return score

    def search(self, query):
        query_words = self.clean_text(query).split()
        if not query_words:
            return []
        
        scores = defaultdict(float)
        for post_id in range(self.total_docs):
            scores[post_id] = self.bm25_score(query_words, post_id)
        
        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [self.posts[post_id] for post_id, score in sorted_results if score > 0]

if __name__ == "__main__":
    use_db = True  # Set to True to use SQLite database
    engine = SearchEngine(use_db=use_db)
    
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
            print(f"Content: {post['content'][:100]}...")
            print()