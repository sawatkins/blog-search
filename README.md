# blog-search
Blog Search is a search engine focused on indexing personal blog content. It is written in Python and used PostgreSQL as the db and for full text search. The app is deployed as a single-node Kubernetes cluster using k3s. Monitoring and observability are handled through Prometheus/Grafana/Loki in the cluster. 

The list of blogs to index is compiled from [this list of HN blogs](https://github.com/outcoldman/hackernews-personal-blogs) and from [ooh's tech directory](https://ooh.directory/blogs/technology/). I started it mainly to get better at SQL and to learn Kubernetes. 

Future plans are to grealy expand the index and implment a scraper with AWS SQS (in progress).

https://blogsearch.io
