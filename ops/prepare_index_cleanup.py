"""Prepare a reviewable cleanup manifest from the restored backup; never mutate SQL."""

import argparse
from collections import Counter, defaultdict
import html
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg2

from ops.audit_index import read_rows
from scraper.fetching import normalize_url
from scraper.records import NON_ARTICLE_HASHES, clean_title, nonarticle_reason, url_variant_key


def repair_url(url):
    normalized = normalize_url(url)
    if normalized:
        fragment = urlsplit(url).fragment
        return normalized + ('#' + fragment if fragment else '')
    # Browsers encode spaces in paths, while old scraper rows sometimes kept
    # them literally. Do not decode % escapes or reorder meaningful queries.
    candidate = url if '://' in url else 'https://' + url
    try:
        parts = urlsplit(candidate)
        candidate = urlunsplit((parts.scheme, parts.netloc,
                               quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                               quote(parts.query, safe="%/?@!$&'()*+,;=:-._~"),
                               quote(parts.fragment, safe="%/?@!$&'()*+,;=:-._~")))
    except ValueError:
        return None
    normalized = normalize_url(candidate)
    fragment = urlsplit(candidate).fragment
    return normalized + ('#' + fragment if fragment else '') if normalized else None


def title_key(title):
    title = unicodedata.normalize('NFKC', html.unescape(title or '')).casefold()
    # Site-name suffix changes do not make an otherwise identical title new.
    title = re.split(r'\s(?:\||—|–|-)\s', title, maxsplit=1)[0]
    return ' '.join(re.findall(r'\w+', title))


def same_article(left, right, texts):
    if left['new_url'] == right['new_url']:
        return 'same_normalized_url'
    if url_variant_key(left['new_url']) != url_variant_key(right['new_url']):
        return None
    if left['content_hash'] == right['content_hash']:
        return 'same_path_and_text'
    title = title_key(left['clean_title'])
    if len(title) >= 10 and title == title_key(right['clean_title']) and left['date'] == right['date']:
        return 'same_path_title_and_date'
    # Review older extractor output without treating navigation shared by two
    # unrelated pages as identity. This is only considered for the same path.
    def shingles(value):
        words = re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold())
        return {tuple(words[i:i + 5]) for i in range(len(words) - 4)}
    a, b = shingles(texts[left['id']]), shingles(texts[right['id']])
    if min(len(a), len(b)) >= 30 and len(a & b) / max(len(a), len(b)) >= 0.8:
        return 'same_path_and_matching_article_body'
    return None


def prepare(directory):
    dsn = os.environ['TEST_DATABASE_URL']
    if urlsplit(dsn).hostname not in {'127.0.0.1', 'localhost'}:
        raise ValueError('Prepare against the restored local backup, not production')
    rows = read_rows(directory / 'database.jsonl.gz')
    proof_path = directory / 'alias-verification.json'
    proofs = json.loads(proof_path.read_text()) if proof_path.exists() else []
    extra_path = directory / 'extra-alias-verification.json'
    extra_proofs = json.loads(extra_path.read_text()) if extra_path.exists() else []
    manual_path = directory / 'reviewed-moves.json'
    manual = json.loads(manual_path.read_text()) if manual_path.exists() else []
    by_id = {r['id']: r for r in rows}
    for proof in manual:
        reviewed = [by_id[i] for i in proof['ids']]
        if (len({r['content_hash'] for r in reviewed}) != 1
                or len({urlsplit(r['url']).netloc for r in reviewed}) != 1
                or proof['preferred_id'] not in proof['ids']):
            raise ValueError('Reviewed moves must share exact text and a host')
    extra_proofs = [p for p in extra_proofs if not any(set(p['ids']) == set(m['ids']) for m in manual)] + manual
    proofs += extra_proofs
    removed = {r['id']: NON_ARTICLE_HASHES[r['content_hash']] for r in rows if r['content_hash'] in NON_ARTICLE_HASHES}
    with psycopg2.connect(dsn) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='10min'")
            cursor.execute("SELECT id,text FROM pages WHERE left(text,45) ILIKE 'Making sure you%'")
            for page_id, text in cursor:
                if reason := nonarticle_reason(text):
                    removed[page_id] = reason
            groups = defaultdict(list)
            repairs = []
            invalid = []
            for row in rows:
                row['new_url'] = repair_url(row['url'])
                if not row['new_url']:
                    invalid.append({k: row[k] for k in ('id', 'url', 'title')})
                    continue
                if row['new_url'] != row['url']:
                    repairs.append({'id': row['id'], 'old_url': row['url'], 'url': row['new_url']})
                if row['id'] not in removed:
                    groups[url_variant_key(row['new_url'])].append(row)
            # Only reviewed, successfully fetched aliases may join groups across
            # different paths/domains. A global equal-body rule loses real posts.
            keys = {row['id']: key for key, group in groups.items() for row in group}
            parents = {}

            def root(key):
                while key in parents:
                    key = parents[key]
                return key

            for proof in extra_proofs:
                joined = [keys[i] for i in proof['ids'] if i in keys]
                if proof['verified'] and joined:
                    for key in joined[1:]:
                        a, b = root(joined[0]), root(key)
                        if a != b:
                            parents[b] = a
            combined = defaultdict(list)
            for key, group in groups.items():
                combined[root(key)].extend(group)
            candidates = [group for group in combined.values() if len(group) > 1]
            ids = [row['id'] for group in candidates for row in group]
            cursor.execute('SELECT id,text FROM pages WHERE id = ANY(%s)', (ids,))
            texts = dict(cursor.fetchall())
    merges, unresolved, title_repairs = [], [], {}
    preferred_ids = {p['preferred_id'] for p in manual}
    for group in candidates:
        group.sort(key=lambda row: (row['id'] in preferred_ids, bool(row['title'] and row['title'].strip()), row['scraped_on_date'] or '',
                                   row['new_url'].startswith('https:'), row['id']), reverse=True)
        keepers = []
        for row in group:
            for keeper in keepers:
                reason = same_article(row, keeper, texts)
                if not reason:
                    proof = next((p for p in proofs if p['verified'] and {row['id'], keeper['id']} <= set(p['ids'])), None)
                    reason = proof['reason'] if proof else None
                if reason:
                    if not (keeper['title'] or '').strip() and (row['title'] or '').strip():
                        title_repairs[keeper['id']] = clean_title(row['title'], keeper['new_url'])
                    merges.append({'source_id': row['id'], 'target_id': keeper['id'], 'source_url': row['url'],
                                   'target_url': keeper['new_url'], 'reason': reason})
                    break
            else:
                keepers.append(row)
        if len(keepers) > 1:
            unresolved.append([{k: row[k] for k in ('id', 'url', 'title', 'date', 'chars')} for row in keepers])
    extra_unresolved = [proof['ids'] for proof in extra_proofs if not proof['verified']]
    plan = {'baseline_pages': len(rows), 'baseline_max_scraped': max(r['scraped_on_date'] or '' for r in rows),
            'merges': merges, 'remove_nonarticles': [{'id': key, 'reason': reason} for key, reason in sorted(removed.items())],
            'url_repairs': repairs, 'title_repairs': [{'id': i, 'title': title} for i, title in title_repairs.items()],
            'unresolved': unresolved, 'extra_unresolved': extra_unresolved, 'invalid_urls': invalid,
            'summary': {'merge_rows': len(merges), 'nonarticle_rows': len(removed), 'url_repairs': len(repairs),
                        'unresolved_groups': len(unresolved), 'extra_unresolved_groups': len(extra_unresolved), 'invalid_urls': len(invalid),
                        'merge_evidence': dict(Counter(row['reason'] for row in merges))}}
    (directory / 'cleanup-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(plan['summary'], indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    prepare(args.directory)
