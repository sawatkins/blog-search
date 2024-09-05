import asyncio
import aiohttp
import feedparser
from bs4 import BeautifulSoup
import json
from playwright.async_api import async_playwright # type: ignore
import os
import sqlite3

async def fetch_feed_content(session, url):
    async with session.get(url, timeout=30) as response:
        response.raise_for_status()
        return await response.text()

async def extract_content_with_readability(page, url):
    await page.goto(url, timeout=30000)
    
    # Read the local Readability.js file
    readability_js_path = os.path.join(os.path.dirname(__file__), 'Readability.min.js')
    
    # Add Readability.js to the page
    await page.add_script_tag(path=readability_js_path)
    
    # Now use Readability to extract the content
    return await page.evaluate('''() => {
        var article = new Readability(document).parse();
        return article ? article.textContent : "";
    }''')

async def process_entry(page, entry):
    post = {
        'title': entry.get('title', ''),
        'url': entry.get('link', ''),
        'date': entry.get('published', '') or entry.get('updated', '')
    }
    try:
        content = await extract_content_with_readability(page, post['url'])
        post['content'] = clean_text(content)
        print(f"Content extracted from URL: {post['url']}")
        return post
    except Exception as e:
        print(f"Error processing entry {post['url']}: {e}")
        return None

async def process_feed(session, url):
    try:
        print(f"Processing feed for URL: {url}")
        feed_content = await fetch_feed_content(session, url)
        feed = feedparser.parse(feed_content)
        
        posts = []
        async with async_playwright() as p:
            browser = await p.firefox.launch()
            page = await browser.new_page()
            
            for entry in feed.entries:
                post = await process_entry(page, entry)
                if post:
                    posts.append(post)
            
            await browser.close()
        
        print(f"URL: {url}\nPosts found: {len(posts)}")
        return posts
    except aiohttp.ClientError as e:
        print(f"Error fetching feed {url}: {e}")
    except Exception as e:
        print(f"Unexpected error processing feed {url}: {e}")
    return []  # Return an empty list if there was an error

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
        print(f"Content: {post['content'][:100]}...")
        print()

# Add this new function to save posts to a JSON file
def save_posts_to_json(posts, filename="all_posts.json"):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(posts, f, ensure_ascii=False, indent=4)
    print(f"Saved {len(posts)} posts to {filename}")

def init_db(db_name='posts.db'):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        url TEXT UNIQUE,
        date TEXT,
        content TEXT
    )
    ''')
    conn.commit()
    return conn

def load_posts_from_json(filename="all_posts.json"):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)
    
    
def save_posts_to_db(posts, conn):
    cursor = conn.cursor()
    for post in posts:
        cursor.execute('''
        INSERT OR REPLACE INTO posts (title, url, date, content)
        VALUES (?, ?, ?, ?)
        ''', (post['title'], post['url'], post['date'], post['content']))
    conn.commit()
    print(f"Saved {len(posts)} posts to the database")


# Modify the main function
async def main():
    urls = read_urls("small-feeds.txt")
    all_posts = await fetch_all_feeds(urls)
    
    # Save posts to JSON file (keeping existing implementation)
    save_posts_to_json(all_posts)
    
    # Optionally save to SQLite database
    # Uncomment the following lines to use SQLite
    conn = init_db()
    save_posts_to_db(all_posts, conn)
    conn.close()
    
    
    # all_posts = load_posts_from_json()
    print_sample_posts(all_posts)

if __name__ == "__main__":
    asyncio.run(main())