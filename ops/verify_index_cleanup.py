"""Verify every retained article against the immutable pre-cleanup export."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from elasticsearch.helpers import scan
import psycopg2
from psycopg2.extras import RealDictCursor

from ops.apply_index_cleanup import protected_snapshot
from ops.audit_index import read_rows
from scraper.records import clean_title
from scraper.scraper import PROJECT, elasticsearch_client, process_lock


def verify(directory, *, live=False):
    plan = json.loads((directory / 'cleanup-plan.json').read_text())
    applied = json.loads((directory / ('applied-live.json' if live else 'rehearsal.json')).read_text())
    if applied['plan_sha256'] != hashlib.sha256((directory / 'cleanup-plan.json').read_bytes()).hexdigest():
        raise RuntimeError('The plan has changed since application')
    removed = {r['source_id'] for r in plan['merges']} | {r['id'] for r in plan['remove_nonarticles']}
    repairs = {r['id']: r['url'] for r in plan['url_repairs']}
    title_repairs = {r['id']: r['title'] for r in plan.get('title_repairs', [])}
    expected = {}
    for row in read_rows(directory / 'database.jsonl.gz'):
        if row['id'] in removed:
            continue
        row['url'] = repairs.get(row['id'], row['url'])
        row['title'] = title_repairs.get(row['id'], clean_title(row['title'], row['url']))
        row['fingerprint'] = row['content_hash']
        expected[row['id']] = row
    dsn = '' if live else os.environ['TEST_DATABASE_URL']
    if not live and urlsplit(dsn).hostname not in {'localhost', '127.0.0.1'}:
        raise ValueError('Rehearsal verification requires localhost')
    report = {'target': applied['target'], 'expected_pages': len(expected)}
    with process_lock(), psycopg2.connect(dsn) as connection:
        connection.set_session(readonly=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL statement_timeout='15min'")
            report['pages_old'] = protected_snapshot(cursor)
            assert report['pages_old'] == applied['pages_old_before'], 'Protected archive differs'
            cursor.execute('SELECT count(*) AS count FROM query_logs')
            report['query_logs'] = cursor.fetchone()['count']
            assert report['query_logs'] >= applied['query_logs_before'], 'Query history lost'
            cursor.execute("""SELECT count(*) AS invalid FROM pages
                WHERE url IS NULL OR title IS NULL OR btrim(title)='' OR page_tsv IS NULL""")
            assert cursor.fetchone()['invalid'] == 0, 'Invalid article metadata'
            cursor.execute('SELECT count(*) AS count FROM page_aliases')
            report['aliases'] = cursor.fetchone()['count']
            cursor.execute('SELECT count(*) AS count FROM page_aliases a JOIN pages p ON a.url=p.url')
            assert cursor.fetchone()['count'] == 0, 'Canonical URL/alias overlap'
            cursor.execute('SELECT count(*) AS count FROM crawl_index_jobs')
            report['outbox'] = cursor.fetchone()['count']
            if live:
                assert report['outbox'] == 0, 'Index delivery is incomplete'
            cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            report['tables'] = sorted(r['tablename'] for r in cursor)
            assert 'feeds' not in report['tables'] and 'skipped_urls' not in report['tables']
        seen = set()
        with connection.cursor(name='verify_pages', cursor_factory=RealDictCursor) as cursor:
            cursor.itersize = 1000
            # Hash inside PostgreSQL: verify all article text without sending
            # another 2GB of public text across the Neon connection.
            cursor.execute("""SELECT id,title,url,original_url,date,fingerprint,
                encode(sha256(convert_to(text,'UTF8')),'hex') AS text_hash FROM pages ORDER BY id""")
            for row in cursor:
                old = expected.get(row['id'])
                assert old is not None, f"Unexpected PostgreSQL ID {row['id']}"
                for key in ('title', 'url', 'original_url', 'fingerprint', 'text_hash'):
                    assert row[key] == old[key], f"PostgreSQL mismatch: {row['id']} {key}"
                assert (str(row['date']) if row['date'] else None) == old['date'], f"Date changed: {row['id']}"
                seen.add(row['id'])
                if len(seen) % 25000 == 0:
                    print(f'PostgreSQL: verified {len(seen)} complete article hashes and metadata', flush=True)
        assert seen == expected.keys(), 'PostgreSQL IDs differ'
        report['postgresql_verified'] = len(seen)
        if live:
            with elasticsearch_client() as es:
                es.indices.refresh(index='pages')
                seen = set()
                for hit in scan(es, index='pages', size=500, query={'query': {'match_all': {}}}, request_timeout=60):
                    page_id = int(hit['_id'])
                    old = expected.get(page_id)
                    assert old is not None, f'Unexpected Elasticsearch ID {page_id}'
                    page = hit['_source']
                    for key in ('title', 'url'):
                        assert page.get(key) == old[key], f'Elasticsearch mismatch: {page_id} {key}'
                    assert hashlib.sha256((page.get('text') or '').encode()).hexdigest() == old['text_hash'], f'Elasticsearch text differs: {page_id}'
                    assert (str(page['date'])[:10] if page.get('date') else None) == old['date'], f'Elasticsearch date differs: {page_id}'
                    seen.add(page_id)
                    if len(seen) % 25000 == 0:
                        print(f'Elasticsearch: verified {len(seen)} complete articles', flush=True)
                assert seen == expected.keys(), 'Elasticsearch IDs differ'
                report['elasticsearch_verified'] = len(seen)
                report['index_health'] = es.cluster.health(index='pages')['status']
    filename = 'verified-live.json' if live else 'verified-rehearsal.json'
    (directory / filename).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    load_dotenv(PROJECT / '.env')
    os.umask(0o077)
    verify(args.directory, live=args.live)
