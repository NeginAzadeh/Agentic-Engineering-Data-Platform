"""Offline container smoke harness: mock HTTP only, execute all real stages.

Mount this test file read-only at /validation/fixture.py and use --network none.
It is excluded from the runtime image and is never discovered as a unit test.
"""

import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import requests

from containerization import run_pipeline as entry


def fixture_response(url, invalid=False, **kwargs):
    records = [
        {'number': 1, 'title': 'Synthetic open PR', 'state': 'open',
         'user': {'login': 'fixture-author'}, 'created_at': '2026-10-01T00:00:00Z',
         'updated_at': '2026-10-02T00:00:00Z', 'closed_at': None, 'merged_at': None},
        {'number': 2, 'title': 'Synthetic merged PR', 'state': 'closed',
         'user': {'login': 'fixture-author'}, 'created_at': '2026-10-01T00:00:00Z',
         'updated_at': '2026-10-01T02:00:00Z',
         'closed_at': '2026-10-01T02:00:00Z', 'merged_at': '2026-10-01T02:00:00Z'},
    ]
    if invalid:
        records[1]['created_at'] = 'invalid timestamp'
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = json.dumps(records).encode()
    return response


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('--ingest-fixture', '--ingest-invalid'):
        invalid = sys.argv[1] == '--ingest-invalid'
        with patch('requests.get', side_effect=lambda url, **kw: fixture_response(url, invalid=invalid, **kw)), \
                patch.object(sys, 'argv', ['ingest_github']):
            runpy.run_module('src.engineering_intelligence.ingest_github', run_name='__main__')
        return 0
    invalid = sys.argv[1:] == ['--invalid']
    original = entry.command_for

    def command(stage, runtime):
        if stage == 'ingest_github':
            return [str(runtime.python), str(Path(__file__)),
                    '--ingest-invalid' if invalid else '--ingest-fixture']
        return original(stage, runtime)

    with patch.object(entry, 'command_for', side_effect=command):
        return entry.main([])


if __name__ == '__main__':
    raise SystemExit(main())
