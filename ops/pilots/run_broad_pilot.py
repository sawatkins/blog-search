"""Bounded live coverage experiment; never loads .env or writes production tables.

Run from repository root with TEST_DATABASE_URL pointing to disposable local
PostgreSQL and PYTHONPATH=. Each run uses a new schema and temporary ES index.
Public response fixtures/reports go to an explicit new output directory.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from elasticsearch import Elasticsearch
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
from psycopg2.extras import RealDictCursor

from scraper.content import SMALLCOMIC_URL, SMALLWEB_URL, feed_jobs, job, source_jobs
from scraper.fetching import FetchError, Fetcher
from scraper.scraper import ensure_index
from scraper.storage import Store
from scraper.worker import Worker, flush_index


class RecordingFetcher(Fetcher):
    def __init__(self, output):
        super().__init__(cache_path=output / 'http-cache.sqlite3')
        self.output = output
        self.records = []
        self.requests = []
        self.record_lock = threading.Lock()

    def _request(self, url, headers, deadline, delay, robots):
        result, location = super()._request(url, headers, deadline, delay, robots)
        with self.record_lock:
            self.requests.append({'url': url, 'status': result.status, 'location': location, 'robots': robots})
        return result, location

    def fetch(self, url, **kwargs):
        record = {'url': url}
        started = time.monotonic()
        try:
            result = super().fetch(url, **kwargs)
            record.update({k: v for k, v in asdict(result).items() if k != 'body'})
            record['requested_url'] = url
            record['bytes'] = len(result.body)
            if result.body:
                filename = sha256(url.encode()).hexdigest() + '.body'
                (self.output / filename).write_bytes(result.body)
                record['fixture'] = filename
            return result
        except FetchError as error:
            record.update(error=str(error), retryable=error.retryable, status=error.status_code,
                          failure_kind=error.failure_kind)
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 3)
            with self.record_lock:
                self.records.append(record)


def select_feeds(urls):
    rng = random.Random(20260907)
    groups = {
        'feedburner': lambda u: 'feedburner.com/' in u,
        'blogspot': lambda u: '.blogspot.com/' in u,
        'wordpress': lambda u: '.wordpress.com/' in u,
        'bearblog': lambda u: '.bearblog.dev/' in u,
        'microblog': lambda u: '.micro.blog/' in u,
        'json': lambda u: urlsplit(u).path.endswith('.json'),
        'subdirectory': lambda u: '/blog/' in u,
        'ghost': lambda u: '.ghost.io/' in u,
        'tumblr': lambda u: '.tumblr.com/' in u,
    }
    selected = []
    used = set()
    for group, predicate in groups.items():
        candidates = [u for u in urls if predicate(u) and u not in used]
        for url in rng.sample(candidates, min(3, len(candidates))):
            selected.append({'url': url, 'group': group})
            used.add(url)
    for url in rng.sample([u for u in urls if u not in used], 48 - len(selected)):
        selected.append({'url': url, 'group': 'random'})
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--minutes', type=float, default=12)
    args = parser.parse_args()
    dsn = os.environ['TEST_DATABASE_URL']
    if urlsplit(dsn).hostname not in {'127.0.0.1', 'localhost'}:
        parser.error('This pilot only accepts a disposable localhost database')
    args.output.mkdir(exist_ok=False)
    report = {'seed': 20260907, 'feeds': [], 'historical_budget_per_root': 12, 'recent_per_feed': 2}
    fetcher = RecordingFetcher(args.output)
    schema = 'broad_pilot_' + uuid4().hex
    index = 'crawler-broad-pilot-' + uuid4().hex
    connection = psycopg2.connect(dsn)
    connection.autocommit = True
    cursor = connection.cursor(cursor_factory=RealDictCursor)
    cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    report.update(schema=schema, index=index)
    # Leave this disposable schema for inspection; the whole test container is
    # removed after its report has been reviewed. No DROP targets from user input.
    store = Store(make_dsn(dsn, options='-csearch_path=' + schema))
    client = Elasticsearch('http://127.0.0.1:9200', request_timeout=20, max_retries=0)
    try:
        store.migrate()
        ensure_index(client, index)
        client.indices.put_settings(index=index, settings={'number_of_replicas': 0})
        web = fetcher.fetch(SMALLWEB_URL).body
        comics = fetcher.fetch(SMALLCOMIC_URL).body
        (args.output / 'smallweb.txt').write_bytes(web)
        (args.output / 'smallcomic.txt').write_bytes(comics)
        urls = [t['url'] for t in source_jobs(web.decode('utf-8-sig'),
                [t['url'] for t in source_jobs(comics.decode('utf-8-sig'))])]
        report['eligible_feeds'] = len(urls)
        selected = select_feeds(urls)
        report['selected'] = selected
        (args.output / 'selection.json').write_text(json.dumps(selected, indent=2))

        def inspect_feed(selected):
            row = dict(selected)
            try:
                response = fetcher.fetch(row['url'])
                tasks, root = feed_jobs(response.body, response.url)
                pages = [task for task in tasks if task['kind'] == 'page']
                row.update(final_url=response.url, root=root, entries=len(pages),
                           feed_urls=[task['url'] for task in pages], format='json' if response.body.lstrip().startswith(b'{') else 'xml')
                seeds = pages[:2] + [task for task in tasks if task['kind'] != 'page']
                if root:
                    store.set_backfill_budget(root, 12)
                    try:
                        seeds.extend(job('sitemap', u, root=root, priority=30) for u in fetcher.robots_sitemaps(root))
                    except FetchError as error:
                        row['sitemap_robots_error'] = str(error)
                store.enqueue([task for task in seeds if task])
            except Exception as error:
                row['error'] = str(error) if isinstance(error, FetchError) else type(error).__name__
            return row

        with ThreadPoolExecutor(max_workers=8) as executor:
            for future in as_completed([executor.submit(inspect_feed, row) for row in selected]):
                row = future.result()
                report['feeds'].append(row)
                print(json.dumps({'feed': row['url'], 'entries': row.get('entries'), 'error': row.get('error')}), flush=True)
        (args.output / 'feeds.json').write_text(json.dumps(report['feeds'], indent=2))
        worker = Worker(store, fetcher, workers=8)
        last_tick = 0

        def tick():
            nonlocal last_tick
            if time.monotonic() - last_tick > 20:
                last_tick = time.monotonic()
                flush_index(store, client, index)
                print(json.dumps({'progress': store.status(), 'fetches': len(fetcher.records)}, default=str), flush=True)

        report['jobs_processed'] = worker.run(max_jobs=700, deadline=time.monotonic() + args.minutes * 60, tick=tick)
        while flush_index(store, client, index):
            pass
        client.indices.refresh(index=index)
        with store._transaction() as cur:
            cur.execute('SELECT id, url, title, date, text, scraped_on_date FROM pages ORDER BY id')
            pages = [dict(row) for row in cur.fetchall()]
            cur.execute('SELECT kind, url, resolved_url, priority, payload, status, attempts, error, due_at FROM crawl_jobs ORDER BY id')
            report['jobs'] = [dict(row) for row in cur.fetchall()]
        mismatches = []
        for start in range(0, len(pages), 100):
            batch = pages[start:start + 100]
            docs = client.mget(index=index, ids=[str(p['id']) for p in batch])['docs']
            for page, doc in zip(batch, docs):
                if not doc.get('found') or any(str(page[k]) != str(doc['_source'][k]) for k in ('url', 'title', 'date', 'text')):
                    mismatches.append(page['id'])
        report.update(status=store.status(), pages=len(pages), indexed=client.count(index=index)['count'],
                      index_mismatches=mismatches,
                      outcomes=dict(Counter((row['status'] + ': ' + (row['error'] or 'ok')) for row in report['jobs'])))
        (args.output / 'pages.json').write_text(json.dumps(pages, indent=2, default=str))
    finally:
        report['fetches'] = fetcher.records
        report['http_requests'] = fetcher.requests
        (args.output / 'report.json').write_text(json.dumps(report, indent=2, default=str))
        if client.indices.exists(index=index):
            client.indices.delete(index=index)
        client.close()
        store.close()
        cursor.close()
        connection.close()
        fetcher.close()
    print(json.dumps({k: report.get(k) for k in ('jobs_processed', 'pages', 'indexed', 'index_mismatches', 'outcomes')}, default=str), flush=True)


if __name__ == '__main__':
    main()
