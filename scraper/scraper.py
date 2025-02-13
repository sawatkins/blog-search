import requests
from urllib.parse import urlparse
import fastfeedparser

class Scraper:
    def __init__(self) -> None:
        pass
    
    def start_initial_scrape(self):
        smallweb_feeds_url = "https://raw.githubusercontent.com/kagisearch/smallweb/refs/heads/main/smallweb.txt"
        smallweb_feeds = []
        
        response = requests.get(smallweb_feeds_url)
        if response.status_code == 200:
            smallweb_feeds = response.text.splitlines()
        else:
            raise Exception(f"Failed to download smallweb feeds: {response.status_code}")
        
        #print(smallweb_feeds[:5])
        return smallweb_feeds[532:537]
        
    def get_url_path_components(self, url):        
        parsed_path = urlparse(url)
        path_elements = [x for x in parsed_path.path.split('/') if x]
        return path_elements

    def get_sample_urls(self, feed):
        try:
            parsed_feed = fastfeedparser.parse(feed)
        except:
            print(f"Failed to parse feed: {feed}")
            return []
        urls = []
        
        for entry in parsed_feed.entries:
            urls.append(entry.link)
        
        return urls
            
        
        
    
     
if __name__ == "__main__":
    pass
    scraper = Scraper()
    smallweb_feeds = scraper.start_initial_scrape()
    
    for feed in smallweb_feeds:
        print(feed)
        feed_urls = scraper.get_sample_urls(feed)
        print(feed_urls)
        for url in feed_urls:
            print(scraper.get_url_path_components(url))