from time import sleep
import boto3
import os
from dotenv import load_dotenv

class SQSQueue:
    def __init__(self, queue_name='blogsearch-to-scrape'):
        try:
            self.sqs_client = boto3.client(
                'sqs',
                region_name='us-west-2',
                aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY')
            )
            
            response = self.sqs_client.get_queue_url(QueueName=queue_name)
            self.queue_url = response['QueueUrl']
            print(f"Connected to queue: {self.queue_url}")
            
        except Exception as e:
            print(f"Error initializing SQS queue: {str(e)}")
            raise
    
    def send_message(self, url):
        try:
            response = self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=url
            )
            return response
        except Exception as e:
            print(f"Error sending message to queue: {str(e)}")
            return None
    
    def receive_message(self, max_messages=1, wait_time_seconds=10):
        try:
            response = self.sqs_client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds
            )
            
            if 'Messages' in response:
                return response['Messages']
            return []
            
        except Exception as e:
            print(f"Error receiving message from queue: {str(e)}")
            return []
    
    def delete_message(self, receipt_handle):
        try:
            self.sqs_client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle
            )
            return True
        except Exception as e:
            print(f"Error deleting message from queue: {str(e)}")
            return False
    
    def change_message_visibility(self, receipt_handle, seconds=10):
        try:
            self.sqs_client.change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=seconds
            )
            return True
        except Exception as e:
            print(f"Error changing message visibility: {str(e)}")
            return False
    
    def purge_queue(self):
        try:
            self.sqs_client.purge_queue(QueueUrl=self.queue_url)
            print(f"Queue {self.queue_url} purged successfully")
            return True
        except Exception as e:
            print(f"Error purging queue: {str(e)}")
            return False

if __name__ == "__main__":
    load_dotenv()
    q = SQSQueue()
    
    q.send_message("https://www.google.com")
    message = q.receive_message()
    if message:
        print("received:", message[0]['Body'])
        q.delete_message(message[0]['ReceiptHandle'])
        print("message deleted after processing")
