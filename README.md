# blog-search
Blog Search is a search engine focused on indexing personal blog content. It can be used to find authentic, personal content from real people sharing their ideas and experiences 

It is written in Python and uses PostgreSQL as the db and for full text search. The app is deployed as a single-node Kubernetes cluster using k3s. Monitoring and observability are handled through Prometheus/Grafana/Loki in the cluster. The scraper is multi-threaded with an AWS SQS queue.

The current list of blogs to index is compiled from Kagi's [smallweb project](https://github.com/kagisearch/smallweb). It is a personal project I started mainly to get better at SQL and learn Kubernetes. 


https://blogsearch.io
