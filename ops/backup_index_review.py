"""Make private, restorable backups before the explicitly requested index cleanup."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from dotenv import load_dotenv

from scraper.scraper import PROJECT, elasticsearch_client, process_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    load_dotenv(PROJECT / '.env')
    os.umask(0o077)
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    dump = args.directory / 'public-before.dump'
    variables = [key for key in os.environ if key.startswith('PG')]
    command = ['docker', 'run', '--rm', '--network', 'host']
    for key in variables:
        command.extend(['-e', key])  # Values stay out of process arguments and logs.
    command.extend(['postgres:17-alpine', 'pg_dump', '--no-owner', '--no-acl',
                    '--schema=public', '--format=custom', '--compress=1'])
    snapshot = 'before-index-cleanup-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    with process_lock(), elasticsearch_client() as es:
        es.snapshot.create(repository='blogsearch_local', snapshot=snapshot,
                           indices='pages', include_global_state=False, wait_for_completion=False)
        print('Backing up the public schema, including pages_old and legacy tables.', flush=True)
        with dump.open('xb') as output, (args.directory / 'pg-dump.log').open('x') as errors:
            result = subprocess.run(command, stdout=output, stderr=errors)
        if result.returncode:
            raise RuntimeError('Database backup failed; inspect the private pg-dump.log')
        while True:
            state = es.snapshot.get(repository='blogsearch_local', snapshot=snapshot)['snapshots'][0]
            if state['state'] != 'IN_PROGRESS':
                break
            time.sleep(2)
        if state['state'] != 'SUCCESS':
            raise RuntimeError('Elasticsearch backup did not complete successfully')
        with dump.open('rb') as data:
            digest = hashlib.file_digest(data, 'sha256').hexdigest()
        manifest = {'database_dump': dump.name, 'bytes': dump.stat().st_size, 'sha256': digest,
                    'snapshot_repository': 'blogsearch_local', 'snapshot': snapshot,
                    'snapshot_state': state['state']}
        (args.directory / 'backup.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(json.dumps(manifest), flush=True)


if __name__ == '__main__':
    main()
