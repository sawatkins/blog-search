from time import sleep
import boto3

class SQSQueue:
    def __init__(self) -> None:
        self.sqs = boto3.resource('sqs', region_name='us-west-2')
        self.queue = self.sqs.get_queue_by_name(QueueName='blogsearch-to-scrape')
        print("got queue:", self.queue.url)
    
    def send_message(self, url):
        try:
            return self.queue.send_message(MessageBody=url)
        except Exception as e:
            print(f"Error sending message to queue: {str(e)}")
            return None
    
    def process_message(self):
        message = self.queue.receive_messages(MaxNumberOfMessages=1)
        if message:
            return message[0].body
        return None
    

if __name__ == "__main__":
    q = SQSQueue()
    m = "hello world! this works!!"
    q.send_message(m)
    print("sent to queue:", m)
    sleep(1)
    returned_message = q.process_message()
    print(returned_message)
