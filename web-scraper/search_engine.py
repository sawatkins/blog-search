from collections import defaultdict
from main import load_posts_from_json
import sqlite3
import re

class SearchEngine:
    def __init__(self, use_db=False, db_name='posts.db'):
        self.index = defaultdict(set)
        self.use_db = use_db
        self.db_name = db_name
        self.posts = self.load_posts()
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
        return re.sub(r'\s+', ' ', text.replace('\n', ' '), flags=re.MULTILINE).strip()

    def create_index(self):
        for post_id, post in enumerate(self.posts):
            title = self.clean_text(post['title'].lower())
            content = self.clean_text(post['content'].lower())
            words = title.split() + content.split()
            for word in words:
                self.index[word].add(post_id)
        print(f"Indexed {len(self.posts)} posts")

    def search(self, query):
        query_words = query.lower().split()
        if not query_words:
            return []
        
        result_set = set.intersection( #wtf does this do?
            *[self.index.get(word, set()) for word in query_words]
        )
        return [self.posts[post_id] for post_id in result_set]

# Example usage
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