import asyncio
import aiohttp
import feedparser
from typing import List, Dict
from bs4 import BeautifulSoup

async def fetch_feed(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as response:
        return await response.text()

def clean_content(html_content: str) -> str:
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator=' ', strip=True)

def parse_feed(feed_content: str) -> List[Dict[str, str]]:
    feed = feedparser.parse(feed_content)
    return [
        {
            'title': entry.get('title', ''),
            'url': entry.get('link', ''),
            'summary': clean_content(entry.get('summary', '')),
            'content': clean_content(entry.get('content', [{}])[0].get('value', '') if entry.get('content') else ''),
            'date': entry.get('published', '') or entry.get('updated', '')
        }
        for entry in feed.entries
    ]

async def process_feed(session: aiohttp.ClientSession, url: str) -> List[Dict[str, str]]:
    feed_content = await fetch_feed(session, url)
    posts = parse_feed(feed_content)
    print(f"URL: {url}\nPosts found: {len(posts)}")
    return posts

async def fetch_all_feeds(urls: List[str]) -> List[Dict[str, str]]:
    async with aiohttp.ClientSession() as session:
        tasks = [process_feed(session, url) for url in urls]
        results = await asyncio.gather(*tasks)
    return [post for posts in results for post in posts]

def read_urls(file_path: str) -> List[str]:
    with open(file_path, 'r') as file:
        return [line.strip() for line in file]

def print_sample_posts(posts: List[Dict[str, str]], sample_size: int = 5):
    print(f"\nTotal posts found: {len(posts)}")
    print(f"\nSample of {sample_size} posts:")
    for post in posts[:sample_size]:
        print(f"Title: {post['title']}")
        print(f"URL: {post['url']}")
        print(f"Date: {post['date']}")
        print(f"Summary: {post['summary'][:100]}...")
        print(f"Content: {post['content'][:100]}...")
        print()

async def main():
    urls = read_urls("small-feeds.txt")
    all_posts = await fetch_all_feeds(urls)
    print_sample_posts(all_posts)

if __name__ == "__main__":
    asyncio.run(main())