# Blog Search

[Blog Search](https://blogsearch.io) indexes personal blogs and independent websites.
Blog sources come from [Kagi Small Web](https://github.com/kagisearch/smallweb).

- **Web:** FastAPI and Jinja, with Elasticsearch for search and PostgreSQL for content.
- **Crawler:** daily feed checks plus resumable sitemap/archive discovery. PostgreSQL
  stores jobs, retry state, HTTP validators, and pending search-index writes.
- **Extraction:** Trafilatura runs locally. No paid extraction API is required.
- **Hosting:** systemd runs the app and scheduled crawler on the existing server;
  Elasticsearch and Plausible run in Docker; the app's database is on Neon.

## Development

```sh
uv sync --frozen
uv run python -m scraper.scraper migrate
uv run python web/server.py
```

Configure PostgreSQL with `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, and
optional `PGPORT`, `PGSSLMODE`, `PGCHANNELBINDING` in a local `.env`.
Set `ELASTICSEARCH_URL` for search (defaults to `http://localhost:9200`).
Database schema installation is explicit; importing or starting the web app never
creates tables.

## Crawling

```sh
uv run python -m scraper.scraper sync
uv run python -m scraper.scraper run --max-jobs 20 --workers 2
uv run python -m scraper.scraper status
```

See [server setup and recovery](ops/README.md) for the production pilot,
daily schedules, backfills, configuration, and safe reindexing.

## Tests

```sh
uv run python -B -m unittest discover -s tests
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database to include transaction
and recovery tests. These tests never use the application's database configuration.
