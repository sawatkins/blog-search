# web-scraper/main.py
import asyncio
import aiohttp
import feedparser
from io import BytesIO

async def fetch_url(session, url):
    async with session.get(url) as response:
        content = await response.read()
        feed = feedparser.parse(BytesIO(content))
        posts = []
        for entry in feed.entries:
            posts.append({
                'title': entry.get('title', ''),
                'url': entry.get('link', '')
            })
        print(f"URL: {url}\nPosts found: {len(posts)}\n")
        return posts

async def fetch_all_urls(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_url(session, url) for url in urls]
        results = await asyncio.gather(*tasks)
        return [post for posts in results for post in posts]

def read_urls(urls_file):
    with open(urls_file, 'r') as file:
        urls = [line.strip() for line in file]
        return urls

async def main():
    urls = read_urls("small-feeds.txt")
    all_posts = await fetch_all_urls(urls)
    print(f"Total posts found: {len(all_posts)}")
    for post in all_posts[:5]:  # Print first 5 posts as an example
        print(f"Title: {post['title']}\nURL: {post['url']}\n")

if __name__ == "__main__":
    asyncio.run(main())