import sys
import boto3
import os
import json
from dotenv import load_dotenv


class SQSQueue:
    def __init__(self, queue_name="blogsearch-to-scrape"):
        try:
            session = boto3.Session()
            self.sqs_client = session.client(
                "sqs",
                region_name="us-west-2",
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                config=boto3.session.Config(
                    max_pool_connections=200,
                ),
            )

            response = self.sqs_client.get_queue_url(QueueName=queue_name)
            self.queue_url = response["QueueUrl"]
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
                QueueUrl=self.queue_url, MessageBody=message_body
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
                VisibilityTimeout=visibility_timeout,
            )

            if "Messages" in response:
                return response["Messages"][0]
            return None

        except Exception as e:
            print(f"Error receiving message from queue: {str(e)}")
            return None

    def delete_message(self, receipt_handle):
        try:
            self.sqs_client.delete_message(
                QueueUrl=self.queue_url, ReceiptHandle=receipt_handle
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
                VisibilityTimeout=seconds,
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
            return

    def get_number_of_messages(self):
        try:
            response = self.sqs_client.get_queue_attributes(
                QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
            )
            return int(response["Attributes"]["ApproximateNumberOfMessages"])
        except Exception as e:
            print(f"Error getting number of messages: {str(e)}")
            return 0

    def peek_first_message(self):
        """View the first message without removing it from the queue"""
        try:
            response = self.sqs_client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=0,
                VisibilityTimeout=0,
            )

            if "Messages" in response:
                message = response["Messages"][0]
                body = json.loads(message["Body"])
                return body
            else:
                print("No messages in queue")
                return None

        except Exception as e:
            print(f"Error peeking at message: {str(e)}")
            return None


if __name__ == "__main__":
    load_dotenv()
    q = SQSQueue()

    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "purge":
            q.purge_queue()
        elif command == "num_messages":
            print(q.get_number_of_messages())
        elif command == "peek":
            message = q.peek_first_message()
            if message:
                print(json.dumps(message, indent=2))
        else:
            print("Unknown command")
    else:
        print("Available commands: purge, num_messages, peek")

    # q.purge_queue()
    # q.send_message(["https://www.google.com"])
    # message = q.receive_message()
    # if message:
    #     print("received:", message)
    #     q.delete_message(message['ReceiptHandle'])
    #     print("message deleted after processing")
