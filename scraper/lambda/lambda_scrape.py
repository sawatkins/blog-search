import json
import trafilatura

def lambda_handler(event, context):
    try:
        url = event['Records'][0]['body']
        downloaded_content = trafilatura.fetch_url(url)
        
        if not downloaded_content:
            return {
                'statusCode': 400,
                'body': json.dumps(f"Failed to download content from {url}")
            }
        
        extracted_text = trafilatura.extract(downloaded_content)
        metadata = trafilatura.extract_metadata(downloaded_content)
        
        result = {
            'url': url,
            'content': extracted_text,
            'title': metadata.title if metadata else None,
            'date': metadata.date if metadata else None
        }
        
        # todo: save to db
        
        return {
            'statusCode': 200,
            'body': json.dumps(f"Successfully scraped {url}")
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error: {str(e)}")
        }