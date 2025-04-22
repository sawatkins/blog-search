from time import sleep
import boto3
import os
import json
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
    
    def send_message(self, urls: list[str]):
        try:
            if not urls or len(urls) < 1:
                raise ValueError("At least one URL must be provided")
                
            message_body = json.dumps({"urls": urls})
            
            response = self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message_body
            )
            return response
        except Exception as e:
            print(f"Error sending message to queue: {str(e)}")
            return None
    
    def receive_message(self, wait_time_seconds=10, visibility_timeout=20):
        try:
            response = self.sqs_client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=wait_time_seconds,
                VisibilityTimeout=visibility_timeout
            )
            
            if 'Messages' in response:
                return response['Messages'][0]
            return None
            
        except Exception as e:
            print(f"Error receiving message from queue: {str(e)}")
            return None
    
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
    
    def change_message_visibility(self, receipt_handle, seconds):
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
    q.purge_queue()
    # q.send_message(["https://www.google.com"])
    # message = q.receive_message()
    # if message:
    #     print("received:", message)
    #     q.delete_message(message['ReceiptHandle'])
    #     print("message deleted after processing")
