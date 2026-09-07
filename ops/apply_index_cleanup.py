"""Apply the reviewed manifest transactionally; defaults to the restored test DB.

Production requires --live, a completed backup manifest and the unchanged full
baseline. Never edits pages_old or query_logs. Index changes use the durable outbox.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from ops.audit_index import read_rows
from scraper.records import clean_title
from scraper.scraper import PROJECT, process_lock
from scraper.storage import Store


def protected_snapshot(cursor):
    cursor.execute("""
        SELECT count(*) AS count, md5(string_agg(md5(row_to_json(saved)::text), '' ORDER BY id)) AS checksum
        FROM (SELECT id,title,url,fingerprint,date,text,scraped_on_date FROM pages_old) saved
    """)
    return dict(cursor.fetchone())


def apply(directory, *, live=False):
    backup = json.loads((directory / 'backup.json').read_text())
    if backup['snapshot_state'] != 'SUCCESS':
        raise RuntimeError('A successful backup is required')
    with (directory / backup['database_dump']).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != backup['sha256']:
            raise RuntimeError('Database backup checksum does not match')
    plan = json.loads((directory / 'cleanup-plan.json').read_text())
    plan_hash = hashlib.sha256((directory / 'cleanup-plan.json').read_bytes()).hexdigest()
    if live:
        rehearsal = json.loads((directory / 'rehearsal.json').read_text())
        if rehearsal.get('plan_sha256') != plan_hash or rehearsal['pages_old_before'] != rehearsal['pages_old_after']:
            raise RuntimeError('The exact cleanup plan must pass rehearsal first')
    if plan['invalid_urls']:
        raise RuntimeError('Unresolved invalid URLs require review')
    rows = read_rows(directory / 'database.jsonl.gz')
    repairs = {r['id']: r['url'] for r in plan['url_repairs']}
    title_repairs = {r['id']: r['title'] for r in plan.get('title_repairs', [])}
    removed = {r['id'] for r in plan['remove_nonarticles']} | {r['source_id'] for r in plan['merges']}
    dsn = '' if live else os.environ['TEST_DATABASE_URL']
    if not live and urlsplit(dsn).hostname not in {'127.0.0.1', 'localhost'}:
        raise ValueError('Rehearsal requires a localhost test database')
    report = {'target': 'live' if live else 'rehearsal', 'plan_sha256': plan_hash}
    with process_lock(), Store(dsn) as store:
        store.migrate()
        with store._transaction(crawl_write=True) as cursor:
            cursor.execute("SET LOCAL statement_timeout='20min'")
            cursor.execute("SET LOCAL lock_timeout='5s'")
            cursor.execute('LOCK TABLE pages IN SHARE ROW EXCLUSIVE MODE')
            cursor.execute("SELECT count(*) AS count, max(scraped_on_date) AS latest FROM pages")
            current = cursor.fetchone()
            if current['count'] != plan['baseline_pages'] or str(current['latest']) != plan['baseline_max_scraped']:
                raise RuntimeError('Page baseline changed; repeat audit before cleanup')
            cursor.execute("SELECT count(*) AS count FROM crawl_jobs WHERE status='running'")
            if cursor.fetchone()['count']:
                raise RuntimeError('Crawler jobs are still running')
            print('Checking the protected archive and staging reviewed changes.', flush=True)
            report['pages_old_before'] = protected_snapshot(cursor)
            cursor.execute('SELECT count(*) AS count FROM query_logs')
            report['query_logs_before'] = cursor.fetchone()['count']
            cursor.execute("""CREATE TEMP TABLE cleanup_pages (
                id INTEGER PRIMARY KEY, old_url TEXT, url TEXT, old_title TEXT, title TEXT,
                old_fingerprint TEXT, fingerprint TEXT, remove BOOLEAN) ON COMMIT DROP""")
            stage = [(r['id'], r['url'], repairs.get(r['id'], r['url']), r['title'],
                      title_repairs.get(r['id'], clean_title(r['title'], repairs.get(r['id'], r['url']))), r['fingerprint'],
                      r['content_hash'], r['id'] in removed) for r in rows]
            execute_values(cursor, 'INSERT INTO cleanup_pages VALUES %s', stage, page_size=1000)
            cursor.execute("""SELECT count(*) AS mismatches FROM pages p FULL JOIN cleanup_pages c ON p.id=c.id
                WHERE p.id IS NULL OR c.id IS NULL OR
                (p.url,p.title,p.fingerprint) IS DISTINCT FROM (c.old_url,c.old_title,c.old_fingerprint)""")
            if cursor.fetchone()['mismatches']:
                raise RuntimeError('Baseline metadata changed; no cleanup was applied')
            cursor.execute('CREATE TEMP TABLE cleanup_merges(source_id INTEGER PRIMARY KEY,target_id INTEGER,source_url TEXT) ON COMMIT DROP')
            if plan['merges']:
                execute_values(cursor, 'INSERT INTO cleanup_merges VALUES %s',
                               [(m['source_id'], m['target_id'], m['source_url']) for m in plan['merges']], page_size=1000)
            cursor.execute('SELECT count(*) AS invalid FROM cleanup_merges WHERE target_id = ANY(%s)', (list(removed),))
            if cursor.fetchone()['invalid']:
                raise RuntimeError('A merge target would also be removed')
            cursor.execute("""UPDATE page_aliases a SET page_id=m.target_id FROM cleanup_merges m WHERE a.page_id=m.source_id""")
            cursor.execute("""INSERT INTO page_aliases(url,page_id) SELECT source_url,target_id FROM cleanup_merges
                ON CONFLICT(url) DO UPDATE SET page_id=EXCLUDED.page_id""")
            cursor.execute("""INSERT INTO page_aliases(url,page_id)
                SELECT DISTINCT c.url,m.target_id FROM cleanup_merges m JOIN cleanup_pages c ON c.id=m.source_id
                WHERE c.url<>c.old_url
                ON CONFLICT(url) DO UPDATE SET page_id=EXCLUDED.page_id""")
            print(f'Removing {len(removed)} reviewed duplicate/non-article rows; all have backups.', flush=True)
            report['removed_rows'] = len(store._delete_pages(cursor, sorted(removed)))
            cursor.execute("""UPDATE crawl_jobs job SET etag=NULL,last_modified=NULL,resolved_url=NULL
                WHERE kind='page' AND EXISTS (SELECT 1 FROM cleanup_pages c WHERE c.remove AND job.url=c.old_url)""")
            cursor.execute("""INSERT INTO page_aliases(url,page_id) SELECT old_url,id FROM cleanup_pages
                WHERE NOT remove AND url IS DISTINCT FROM old_url
                ON CONFLICT(url) DO UPDATE SET page_id=EXCLUDED.page_id""")
            print('Standardizing legacy fingerprints without rewriting article text.', flush=True)
            cursor.execute("""UPDATE pages p SET fingerprint=c.fingerprint FROM cleanup_pages c
                WHERE p.id=c.id AND p.fingerprint IS DISTINCT FROM c.fingerprint""")
            report['fingerprints_updated'] = cursor.rowcount
            cursor.execute("""WITH changed AS (
                UPDATE pages p SET title=c.title,url=c.url FROM cleanup_pages c WHERE p.id=c.id
                    AND (p.title,p.url) IS DISTINCT FROM (c.title,c.url) RETURNING p.id)
                INSERT INTO crawl_index_jobs(page_id) SELECT id FROM changed ORDER BY id
                ON CONFLICT(page_id) DO UPDATE SET operation='index', revision=nextval('crawl_index_revision_seq'),
                    due_at=CURRENT_TIMESTAMP,attempts=0,error=NULL""")
            report['metadata_updated'] = cursor.rowcount
            cursor.execute('DELETE FROM page_aliases a USING pages p WHERE a.page_id=p.id AND a.url=p.url')
            cursor.execute('SELECT count(*) AS collisions FROM page_aliases a JOIN pages p ON a.url=p.url WHERE a.page_id<>p.id')
            if cursor.fetchone()['collisions']:
                raise RuntimeError('Alias conflicts with a different active article; rolling back')
            # Known obsolete tables only; never CASCADE into dependent objects.
            report['retired_tables'] = {}
            for table in ('feeds', 'skipped_urls'):
                cursor.execute(f'SELECT count(*) AS count FROM {table}')
                report['retired_tables'][table] = cursor.fetchone()['count']
                cursor.execute(f'DROP TABLE {table}')
            cursor.execute('DROP INDEX IF EXISTS pages_date_idx')
            report['pages_old_after'] = protected_snapshot(cursor)
            if report['pages_old_after'] != report['pages_old_before']:
                raise RuntimeError('Protected archive changed; rolling back')
            cursor.execute('SELECT count(*) AS count FROM pages')
            report['pages_after'] = cursor.fetchone()['count']
            if report['pages_after'] != len(rows) - len(removed):
                raise RuntimeError('Unexpected final page count; rolling back')
            cursor.execute("""SELECT count(*) AS invalid FROM pages
                WHERE url IS NULL OR title IS NULL OR btrim(title)='' OR page_tsv IS NULL""")
            if cursor.fetchone()['invalid']:
                raise RuntimeError('Invalid active records remain; rolling back')
            cursor.execute('SELECT count(*) AS count FROM query_logs')
            report['query_logs_after'] = cursor.fetchone()['count']
            if report['query_logs_after'] < report['query_logs_before']:
                raise RuntimeError('Query logs decreased; rolling back')
        report['outbox'] = store.status()['outbox']
    output = directory / ('applied-live.json' if live else 'rehearsal.json')
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--live', action='store_true', help='Apply the already-rehearsed manifest to this project’s live DB')
    args = parser.parse_args()
    load_dotenv(PROJECT / '.env')
    os.umask(0o077)
    apply(args.directory, live=args.live)
