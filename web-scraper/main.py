# web-scraper/main.py
# 1. Import the necessary libraries
import requests
from bs4 import BeautifulSoup

# 2. Define the URL of the website you want to scrape
url = 'https://example.com'

# 3. Send a GET request to the website
response = requests.get(url)

# 4. Parse the HTML content of the website
soup = BeautifulSoup(response.content, 'html.parser')