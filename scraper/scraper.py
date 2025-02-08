import requests

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
        
        print(smallweb_feeds[:5])

if __name__ == "__main__":
    scraper = Scraper()
    scraper.start_initial_scrape()