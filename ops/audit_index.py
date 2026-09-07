"""Read-only, full-record audit of PostgreSQL and Elasticsearch article identity.

Artifacts contain public article metadata, not query logs or credentials. Prefer
TEST_DATABASE_URL pointing to a restored local backup for the large SQL scan.
"""

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from elasticsearch.helpers import scan
import psycopg2
from psycopg2.extras import RealDictCursor

from scraper.fetching import normalize_url
from scraper.records import clean_title, content_fingerprint, url_variant_key
from scraper.scraper import PROJECT, elasticsearch_client


def metadata(row):
    text = row.get('text') or ''
    url = row.get('url') or ''
    title = row.get('title')
    return {key: row.get(key) for key in ('id', 'url', 'original_url', 'title', 'date', 'scraped_on_date', 'fingerprint')} | {
        'clean_title': clean_title(title, url), 'normalized_url': normalize_url(url),
        'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'content_hash': content_fingerprint(text),
        'chars': len(text),
    }


def emit(path, rows):
    with gzip.open(path, 'wt', encoding='utf-8', compresslevel=1) as stream:
        count = 0
        for row in rows:
            stream.write(json.dumps(metadata(row), ensure_ascii=False, default=str) + '\n')
            count += 1
            if count % 25000 == 0:
                print(f'{path.name}: {count} records', flush=True)
    print(f'{path.name}: completed {count} records', flush=True)


def export_db(directory):
    with psycopg2.connect(os.getenv('TEST_DATABASE_URL', '')) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as settings:
            settings.execute("SET LOCAL statement_timeout='15min'")
        with connection.cursor(name='page_audit', cursor_factory=RealDictCursor) as cursor:
            cursor.itersize = 1000
            cursor.execute('SELECT id,url,original_url,title,date,scraped_on_date,fingerprint,text FROM pages ORDER BY id')
            emit(directory / 'database.jsonl.gz', cursor)


def export_es(directory):
    with elasticsearch_client() as es:
        rows = ({'id': hit['_id'], **hit['_source']} for hit in scan(es, index='pages', size=500,
                query={'query': {'match_all': {}}}, request_timeout=60))
        emit(directory / 'elasticsearch.jsonl.gz', rows)


def read_rows(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]


def compare(directory):
    database = read_rows(directory / 'database.jsonl.gz')
    es = {str(row['id']): row for row in read_rows(directory / 'elasticsearch.jsonl.gz')}
    counts = Counter()
    groups = {key: defaultdict(list) for key in ('url', 'normalized_url', 'content_hash', 'same_article_variants')}
    mismatch, titles, invalid = [], [], []
    for row in database:
        for field, grouped in groups.items():
            if field == 'same_article_variants':
                variant = url_variant_key(row['url'])
                if variant:
                    grouped[(variant, row['content_hash'])].append(row)
            elif row[field]:
                grouped[row[field]].append(row)
        if row['title'] != row['clean_title']:
            counts['title_updates'] += 1
            titles.append({k: row[k] for k in ('id', 'url', 'title', 'clean_title')})
        if not row['normalized_url']:
            counts['invalid_or_legacy_urls'] += 1
            invalid.append({k: row[k] for k in ('id', 'url', 'original_url', 'title')})
        if row['fingerprint'] != row['content_hash']:
            counts['legacy_fingerprints'] += 1
        document = es.pop(str(row['id']), None)
        if document is None:
            counts['missing_es_ids'] += 1
            mismatch.append({'id': row['id'], 'reason': 'missing'})
        else:
            fields = [key for key in ('url', 'title', 'text_hash') if row[key] != document[key]]
            # Elasticsearch dates may include midnight; PostgreSQL stores a date.
            if (str(row['date'])[:10] if row['date'] else None) != (str(document['date'])[:10] if document['date'] else None):
                fields.append('date')
            if fields:
                counts['mismatched_es_documents'] += 1
                mismatch.append({'id': row['id'], 'fields': fields})
    duplicates = {}
    for field, grouped in groups.items():
        duplicate_groups = [rows for rows in grouped.values() if len(rows) > 1]
        counts[field + '_duplicate_groups'] = len(duplicate_groups)
        counts[field + '_duplicate_excess'] = sum(len(rows) - 1 for rows in duplicate_groups)
        duplicates[field] = [[{k: row[k] for k in ('id', 'url', 'title', 'date', 'chars', 'content_hash')}
                              for row in rows] for rows in duplicate_groups]
    counts.update(database_pages=len(database), es_extra_ids=len(es))
    (directory / 'duplicates.json').write_text(json.dumps(duplicates, ensure_ascii=False, indent=2))
    (directory / 'title-updates.json').write_text(json.dumps(titles, ensure_ascii=False, indent=2))
    (directory / 'legacy-urls.json').write_text(json.dumps(invalid, ensure_ascii=False, indent=2))
    (directory / 'index-differences.json').write_text(json.dumps({'mismatch': mismatch, 'extra_ids': list(es)}, indent=2))
    (directory / 'audit.json').write_text(json.dumps(dict(counts), indent=2))
    print(json.dumps(dict(counts), indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['database', 'elasticsearch', 'compare'])
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    load_dotenv(PROJECT / '.env')
    os.umask(0o077)
    {'database': export_db, 'elasticsearch': export_es, 'compare': compare}[args.action](args.directory)


if __name__ == '__main__':
    main()
