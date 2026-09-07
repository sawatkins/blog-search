"""Bounded public-URL verification for ambiguous pairs; no SQL/index writes."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from ops.prepare_index_cleanup import repair_url
from scraper.content import PageRejected, extract_page
from scraper.fetching import FetchError, Fetcher, normalize_url
from scraper.records import content_fingerprint
from scraper.scraper import PROJECT, process_lock, state_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--retry-host-waits', action='store_true', help='Retry only local contention, not dead/blocked endpoints')
    parser.add_argument('--extra-candidates', action='store_true', help='Check the separately reviewed moved-post candidates')
    args = parser.parse_args()
    load_dotenv(PROJECT / '.env')
    os.umask(0o077)
    plan = json.loads((args.directory / 'cleanup-plan.json').read_text())
    previous_report = []
    groups = plan['unresolved']
    if args.extra_candidates:
        groups = json.loads((args.directory / 'extra-candidates.json').read_text())
    if args.retry_host_waits:
        previous_report = json.loads((args.directory / 'alias-verification.json').read_text())
        retries = {tuple(sorted(row['ids'])) for row in previous_report
                   if any(r.get('error') == 'Host wait deadline exceeded' for r in row['results'])}
        groups = [group for group in groups if tuple(sorted(row['id'] for row in group)) in retries]
    if len(groups) > 100:
        raise RuntimeError('This pilot is limited to 100 ambiguous groups')
    with process_lock(), closing(Fetcher(delay=5, cache_path=state_directory() / 'http-cache.sqlite3')) as fetcher:
        def check(group):
            results = []
            for row in reversed(group):
                url = repair_url(row['url'])
                # A prior fetch already demonstrated that this exact URL is its
                # successful final destination; no second request is needed.
                previous = next((r for r in results if r.get('final_url') == url), None)
                if previous:
                    results.append({'id': row['id'], 'requested_url': url, 'final_url': url,
                                    'body_hash': previous['body_hash'], 'already_verified': True})
                    continue
                try:
                    response = fetcher.fetch(url)
                    page = extract_page(response.body, response.url, content_type=response.content_type,
                                        robots_header=response.robots_header)
                    if page is None:
                        raise PageRejected('No readable article for verification')
                    results.append({'id': row['id'], 'requested_url': url, 'final_url': response.url,
                                    'body_hash': content_fingerprint(page['text'])})
                except PageRejected as error:
                    results.append({'id': row['id'], 'requested_url': url, 'error': str(error)})
                    break
                except FetchError as error:
                    results.append({'id': row['id'], 'requested_url': url, 'error': str(error),
                                    'failure_kind': error.failure_kind, 'status': error.status_code})
                    break  # No repeated requests to a failing/blocked site in this check.
            success = len(results) == len(group) and all('final_url' in result for result in results)
            same_final = success and len({normalize_url(result['final_url']) for result in results}) == 1
            same_body = success and len({result['body_hash'] for result in results}) == 1
            return {'ids': [row['id'] for row in group], 'results': results,
                    'verified': same_final or same_body,
                    'reason': 'observed_redirect' if same_final else 'same_live_response' if same_body else None}
        report = []
        hosts = defaultdict(list)
        for group in groups:
            hosts[urlsplit(repair_url(group[0]['url'])).hostname].append(group)

        def check_host(host_groups):
            return [check(group) for group in host_groups]

        with ThreadPoolExecutor(max_workers=8) as executor:
            for results in executor.map(check_host, hosts.values()):
                for result in results:
                    report.append(result)
                    print(f'Checked {len(report)}/{len(groups)} pairs; verified={result["verified"]}', flush=True)
    if previous_report:
        updated = {tuple(sorted(row['ids'])): row for row in report}
        report = [updated.get(tuple(sorted(row['ids'])), row) for row in previous_report]
    filename = 'extra-alias-verification.json' if args.extra_candidates else 'alias-verification.json'
    (args.directory / filename).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'groups': len(report), 'verified': sum(r['verified'] for r in report)}), flush=True)


if __name__ == '__main__':
    main()
