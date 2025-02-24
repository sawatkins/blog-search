from time import sleep
import boto3
import os
from dotenv import load_dotenv

class SQSQueue:
    def __init__(self) -> None:
        load_dotenv()
        self.sqs = boto3.resource(
            'sqs',
            region_name='us-west-2',
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY')
        )
        self.queue = self.sqs.get_queue_by_name(QueueName='blogsearch-to-scrape')
        print("queue:", self.queue.url)
    
    def send_message(self, url):
        try:
            return self.queue.send_message(MessageBody=url)
        except Exception as e:
            print(f"Error sending message to queue: {str(e)}")
            return None
    
    def receive_message(self):
        try:
            message = self.queue.receive_messages(MaxNumberOfMessages=1)
            if message:
                body = message[0].body
                message[0].delete()
                return body
        except Exception as e:
            print(f"Error receiveing message from queue: {str(e)}")
            return None
    

if __name__ == "__main__":
    q = SQSQueue()
    m = "hello world! this works!!"
    q.send_message(m)
    print("sent to queue:", m)
    sleep(1)
    returned_message = q.receive_message()
    print(returned_message)
