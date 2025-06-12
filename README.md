# blog-search
Blog Search is a search engine focused on indexing personal blog content. It can be used to find authentic, personal content from real people sharing their ideas and experiences.

The web app is written in Python using FastAPI. It uses PostgreSQL as the database. I'm testing search results from both PostgreSQL's full text search and with Meilisearch. 

The scraper is multi-threaded and uses an AWS SQS message queue. Both the web app and scraper are deployed on a Linux server with Docker Compose.

The current list of blogs to index is compiled from Kagi's [smallweb project](https://github.com/kagisearch/smallweb). This is a personal project I started mainly to get better at SQL, web scraping, and message queues. 


https://blogsearch.io
