import os
import xml.etree.ElementTree as ET

def parse_opml_files(directory):
    urls = set()
    for filename in os.listdir(directory):
        if filename.endswith('.ompl'):
            file_path = os.path.join(directory, filename)
            tree = ET.parse(file_path)
            root = tree.getroot()
            for outline in root.findall('.//outline'):
                xml_url = outline.get('xmlUrl')
                if xml_url:
                    urls.add(xml_url)
    return urls

def save_urls_to_file(urls, output_file):
    with open(output_file, 'w') as f:
        for url in sorted(urls):
            f.write(f"{url}\n")

if __name__ == "__main__":
    feeds_directory = "feeds"
    output_file = "feed_urls.txt"

    directory = os.path.join(os.path.dirname(__file__), feeds_directory)
    unique_urls = parse_opml_files(directory)
    save_urls_to_file(unique_urls, output_file)
    print(f"Extracted {len(unique_urls)} unique URLs and saved them to {output_file}")
