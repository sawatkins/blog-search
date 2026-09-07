"""Replay captured pilot responses through current Worker/SQL/index code offline.

No public-web fetching, .env loading, production schema or production index.
The input is the output directory of run_broad_pilot.py.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from elasticsearch import Elasticsearch
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from scraper.fetching import FetchError, FetchResult
from scraper.scraper import ensure_index
from scraper.storage import Store
from scraper.worker import Worker, flush_index


class ReplayFetcher:
    def __init__(self, directory, records):
        self.directory = directory
        self.records = {r.get('requested_url', r['url']): r for r in records}
        self.missing = []

    def fetch(self, url, **kwargs):
        if url not in self.records:
            self.missing.append(url)
            raise FetchError('No captured response', retryable=False)
        record = self.records[url]
        if 'error' in record:
            raise FetchError(record['error'], retryable=record['retryable'], status_code=record.get('status'),
                             failure_kind=record.get('failure_kind'))
        return FetchResult(url=record['url'], status=record['status'],
                           body=(self.directory / record['fixture']).read_bytes() if record.get('fixture') else b'',
                           content_type=record.get('content_type', ''), robots_header=record.get('robots_header', ''),
                           etag=record.get('etag'), last_modified=record.get('last_modified'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    dsn = os.environ['TEST_DATABASE_URL']
    if urlsplit(dsn).hostname not in {'localhost', '127.0.0.1'}:
        parser.error('Only a disposable localhost database is supported')
    before = json.loads((args.directory / 'report.json').read_text())
    before_pages = {p['url']: p for p in json.loads((args.directory / 'pages.json').read_text())}
    fresh = {u for feed in before['feeds'] for u in feed.get('feed_urls', [])[:2]}
    tasks = [{k: j[k] for k in ('kind', 'url', 'payload')} | {
        'priority': j.get('priority', 90 if j['kind'] == 'page' and j['url'] in fresh else
                          {'page': 10, 'archive': 20, 'sitemap': 30, 'feed_page': 40}[j['kind']])}
             for j in before['jobs']]
    schema = 'broad_replay_' + uuid4().hex
    index = 'crawler-broad-replay-' + uuid4().hex
    connection = psycopg2.connect(dsn)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    connection.close()
    report = {'schema': schema, 'index': index}
    fetcher = ReplayFetcher(args.directory, before['fetches'])
    with Store(make_dsn(dsn, options='-csearch_path=' + schema)) as store, \
            Elasticsearch('http://127.0.0.1:9200', request_timeout=20, max_retries=0) as client:
        store.migrate()
        store.enqueue(tasks)
        # Freeze admission at this recorded sample, without changing discovery
        # parsing or following new live URLs. Tests cover new-child admission.
        with store._transaction() as cursor:
            cursor.execute('UPDATE crawl_sites SET cap = discovered, limited = true')
        ensure_index(client, index)
        client.indices.put_settings(index=index, settings={'number_of_replicas': 0})
        try:
            report['jobs_processed'] = Worker(store, fetcher).run(max_jobs=1000)
            while flush_index(store, client, index):
                pass
            client.indices.refresh(index=index)
            with store._transaction() as cursor:
                cursor.execute('SELECT id, url, title, date, text FROM pages ORDER BY id')
                pages = [dict(row) for row in cursor.fetchall()]
                cursor.execute('SELECT kind,url,payload,status,error FROM crawl_jobs ORDER BY id')
                jobs = [dict(row) for row in cursor.fetchall()]
            report.update(pages=len(pages), indexed=client.count(index=index)['count'],
                          missing_fixtures=fetcher.missing, status=store.status(),
                          outcomes=dict(Counter(j['status'] + ': ' + (j['error'] or 'ok') for j in jobs)))
            mismatches = []
            for start in range(0, len(pages), 100):
                batch = pages[start:start + 100]
                docs = client.mget(index=index, ids=[str(p['id']) for p in batch])['docs']
                for page, doc in zip(batch, docs):
                    if not doc.get('found') or any(str(page[k]) != str(doc['_source'][k]) for k in ('url', 'title', 'date', 'text')):
                        mismatches.append(page['id'])
            report['index_mismatches'] = mismatches
            report['removed_from_sample'] = sorted(set(before_pages) - {p['url'] for p in pages})
            report['changes'] = [{'url': p['url'], 'before': len(before_pages[p['url']]['text']) if p['url'] in before_pages else None,
                                  'after': len(p['text'])} for p in pages
                                 if p['url'] not in before_pages or p['text'] != before_pages[p['url']]['text']]
            (args.directory / 'replay-pages.json').write_text(json.dumps(pages, indent=2, default=str))
            (args.directory / 'replay-jobs.json').write_text(json.dumps(jobs, indent=2, default=str))
            (args.directory / 'replay.json').write_text(json.dumps(report, indent=2, default=str))
        finally:
            client.indices.delete(index=index)
    print(json.dumps(report, indent=2, default=str))


if __name__ == '__main__':
    main()
