import asyncio
import aiohttp
import feedparser
from bs4 import BeautifulSoup
import json  # Add this import

# Fetch and parse a single feed
async def process_feed(session, url):
    # Fetch the feed content
    async with session.get(url) as response:
        feed_content = await response.text()
    
    # Parse the feed
    feed = feedparser.parse(feed_content)
    
    # Extract relevant information from each entry
    posts = []
    for entry in feed.entries:
        post = {
            'title': entry.get('title', ''),
            'url': entry.get('link', ''),
            'description': clean_text(entry.get('description', '')),
            'content': clean_text(entry.get('content', [{}])[0].get('value', '')),
            'date': entry.get('published', '') or entry.get('updated', '')
        }
        posts.append(post)
    
    print(f"URL: {url}\nPosts found: {len(posts)}")
    return posts # this is a list of dictionaries

# Remove HTML tags and clean text
def clean_text(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator=' ', strip=True)

# Process all feeds concurrently
async def fetch_all_feeds(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [process_feed(session, url) for url in urls]
        results = await asyncio.gather(*tasks) 
    all_posts = []
    for posts in results:
        all_posts.extend(posts)
    return all_posts

# Read URLs from a file
def read_urls(file_path):
    with open(file_path, 'r') as file:
        return [line.strip() for line in file]

# Print a sample of posts
def print_sample_posts(posts, sample_size=10):
    print(f"\nTotal posts found: {len(posts)}")
    print(f"\nSample of {sample_size} posts:")
    for post in posts[:sample_size]:
        print(f"Title: {post['title']}")
        print(f"URL: {post['url']}")
        print(f"Date: {post['date']}")
        print(f"Description: {post['description'][:100]}...")
        print(f"Content: {post['content'][:100]}...")
        print()

# Add this new function to save posts to a JSON file
def save_posts_to_json(posts, filename="all_posts.json"):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(posts, f, ensure_ascii=False, indent=4)
    print(f"Saved {len(posts)} posts to {filename}")


def load_posts_from_json(filename="all_posts.json"):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)

# Main function
async def main():
    # urls = read_urls("small-feeds.txt")
    # all_posts = await fetch_all_feeds(urls)
    # save_posts_to_json(all_posts)  # Save posts to JSON file
    # print_sample_posts(all_posts)

    all_posts = load_posts_from_json()
    print_sample_posts(all_posts)

if __name__ == "__main__":
    asyncio.run(main())