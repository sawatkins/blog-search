from collections import defaultdict
from main import load_posts_from_json

class SearchEngine:
    def __init__(self):
        self.index = defaultdict(set)
        self.posts = load_posts_from_json()
        self.create_index()

    def create_index(self):
        for post_id, post in enumerate(self.posts):
            words = set(post['title'].lower().split() + 
                        post['description'].lower().split() + 
                        post['content'].lower().split())
            for word in words:
                self.index[word].add(post_id)
        print(f"Indexed {len(self.posts)} posts")

    def search(self, query):
        query_words = query.lower().split()
        if not query_words:
            return []
        
        result_set = set.intersection(
            *[self.index.get(word, set()) for word in query_words]
        )
        return [self.posts[post_id] for post_id in result_set]

# Example usage
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
            print(f"Description: {post['description'][:100]}...")
            print()