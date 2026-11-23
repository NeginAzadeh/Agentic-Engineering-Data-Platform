"""One-shot, read-only inspection of existing local pipeline evidence."""

import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import duckdb

from orchestration.pipeline_runtime import STAGES
from .agent_tools import AgentTools, LIMITATIONS


ROOT = Path(__file__).resolve().parents[2]
DAG_ID = 'engineering_data_pipeline'


def timestamp(value, naive_utc=False):
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.year == 1:  # Docker's unset timestamp sentinel.
        return None
    if parsed.tzinfo is None:
        if not naive_utc:
            raise ValueError('Timestamp requires an explicit timezone')
        parsed = parsed.replace(tzinfo=timezone.utc)  # Airflow stores naive UTC in SQLite.
    return parsed.astimezone(timezone.utc)


def duration(start, finish, naive_utc=False):
    start, finish = timestamp(start, naive_utc), timestamp(finish, naive_utc)
    if start is None or finish is None:
        return None
    elapsed = (finish - start).total_seconds()
    if elapsed < 0:
        raise ValueError('Finish precedes start')
    return elapsed


def read_json(path):
    def reject_constant(value):
        raise ValueError(f'Non-finite JSON value: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=reject_constant)


def warehouse_status(data_dir, checked_at):
    """Reuse existing tool freshness/quality definitions; never run the loader."""
    database = data_dir / 'processed/engineering.duckdb'
    if not database.is_file():
        return {'status': 'unknown', 'path': str(database), 'error': 'warehouse_missing', 'repositories': []}
    with duckdb.connect(str(database), read_only=True) as connection:
        repositories = connection.execute('''
            SELECT repository FROM ingestion_runs UNION SELECT repository FROM pull_requests
            ORDER BY repository
        ''').fetchall()
    tools = AgentTools(database, now=lambda: checked_at)
    results = [tools.get_data_quality_status(repository) for repository, in repositories]
    return {'status': 'available' if repositories else 'empty', 'path': str(database),
            'repositories': results, 'meaning': 'Current stored observations; not proof of a selected execution outcome.'}


def airflow_status(database, run_id):
    """Inspect one explicit run in the existing standalone SQLite metadata DB."""
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only = ON')
        run = connection.execute('''
            SELECT dag_id, run_id, state, start_date, end_date FROM dag_run
            WHERE dag_id = ? AND run_id = ?
        ''', [DAG_ID, run_id]).fetchone()
        if run is None:
            raise ValueError('Requested Airflow run not found')
        rows = connection.execute('''
            SELECT task_id, state, start_date, end_date, try_number FROM task_instance
            WHERE dag_id = ? AND run_id = ? ORDER BY task_id
        ''', [DAG_ID, run_id]).fetchall()
    tasks = []
    for row in rows:
        item = dict(row)
        item['duration_seconds'] = duration(row['start_date'], row['end_date'], naive_utc=True)
        tasks.append(item)
    tasks.sort(key=lambda item: STAGES.index(item['task_id']) if item['task_id'] in STAGES else len(STAGES))
    state = run['state']
    if state == 'failed' or any(t['state'] in ('failed', 'upstream_failed') for t in tasks):
        status = 'failed'
    elif state in ('running', 'queued'):
        status = 'in_progress'
    elif (state == 'success' and len(tasks) == len(STAGES)
          and {t['task_id'] for t in tasks} == set(STAGES)
          and all(t['state'] == 'success' for t in tasks)):
        status = 'success'
    else:
        status = 'unknown'
    return {'status': status, 'source': str(database), **dict(run), 'tasks': tasks,
            'duration_seconds': duration(run['start_date'], run['end_date'], naive_utc=True),
            'timestamps_meaning': 'Airflow SQLite timestamps are UTC; missing durations remain null.',
            'log_hint': 'Inspect this run and each attempt in the existing Airflow UI/task logs.'}


def docker_status(inspect_path=None, result_path=None, log_path=None):
    """Consume selected existing exports; no Docker daemon or shell execution."""
    output = {'status': 'unknown', 'duration_seconds': None, 'stages': [], 'warnings': []}
    if inspect_path:
        raw = read_json(inspect_path)
        if isinstance(raw, list):
            if len(raw) != 1:
                raise ValueError('Export exactly one container with docker inspect')
            raw = raw[0]
        state = raw['State']
        code = state.get('ExitCode')
        status = state.get('Status')
        if state.get('OOMKilled') or state.get('Error') or (status == 'exited' and type(code) is int and code != 0):
            outcome = 'failed'
        elif status in ('running', 'restarting', 'paused', 'created'):
            outcome = 'in_progress'
        elif status == 'exited' and type(code) is int and code == 0:
            outcome = 'success'
        else:
            outcome = 'unknown'
        output.update(status=outcome, source=str(inspect_path), container_id=raw.get('Id'),
                      container_state=status, exit_code=code, error=state.get('Error'),
                      oom_killed=state.get('OOMKilled'),
                      duration_seconds=duration(state.get('StartedAt'), state.get('FinishedAt')))
        # Deliberately exclude Config.Env and other possible credentials.
    elif result_path:
        raw = read_json(result_path)  # Existing Docker milestone live-results.json format.
        code, elapsed = raw['compose_exit'], raw.get('elapsed_seconds')
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError('Invalid Compose exit code')
        if elapsed is not None and (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError('Invalid Compose duration')
        output.update(status='success' if code == 0 else 'failed', source=str(result_path),
                      exit_code=code, duration_seconds=elapsed, data_directory=raw.get('data_directory'))
    stages = {name: {'stage': name, 'status': 'not_observed', 'duration_seconds': None} for name in STAGES}
    if log_path:
        output['log_source'] = str(log_path)
        output['warnings'].append('Docker log/container association is caller-selected, not authenticated.')
        for line in Path(log_path).read_text(encoding='utf-8').splitlines():
            # Docker logs --timestamps prefixes RFC3339Nano. Legacy logs have no prefix.
            parts = line.split(' ', 1)
            when = None
            if len(parts) == 2 and re.match(r'^\d{4}-\d{2}-\d{2}T', parts[0]):
                when = timestamp(parts[0])
                line = parts[1]
            match = re.fullmatch(r'(Starting|Completed) (\w+)', line)
            if match and match[2] in stages:
                item = stages[match[2]]
                key = 'started_at' if match[1] == 'Starting' else 'finished_at'
                if key in item:
                    raise ValueError('Repeated stage markers: export one execution only')
                item[key] = when.isoformat() if when else None
                item['status'] = 'started' if match[1] == 'Starting' else 'completion_observed'
                item['duration_seconds'] = duration(item.get('started_at'), item.get('finished_at'))
            if line.startswith(('Pipeline stage failed', 'Pipeline timed out:', 'Pipeline failed:')):
                output['status'] = 'failed'
        if output['status'] == 'failed':
            for item in stages.values():
                if item['status'] == 'started':
                    item['status'] = 'failed_or_interrupted'
    output['stages'] = list(stages.values())
    if any(item['duration_seconds'] is None for item in stages.values()):
        output['warnings'].append('Stage timing is unavailable without matching timestamped start/end markers.')
    if output['status'] == 'success' and any(t['status'] != 'completion_observed' for t in stages.values()):
        output['warnings'].append('Exit zero is observed, but completion of all four pipeline stages is not evidenced.')
        output['status'] = 'unknown'
    return output


def dbt_status(path):
    raw = read_json(path)
    generated = timestamp(raw['metadata']['generated_at'])
    results = raw['results']
    if generated is None or not isinstance(results, list) or not results:
        raise ValueError('dbt result metadata/results missing')
    counts = Counter(item['status'] for item in results)
    state = 'failed' if counts['fail'] or counts['error'] else (
        'success' if set(counts) <= {'pass', 'success'} else 'unknown')
    return {'status': state, 'source': str(path), 'generated_at_utc': generated.isoformat(),
            'invocation_id': raw['metadata'].get('invocation_id'), 'counts': dict(counts),
            'nodes': [{'id': item['unique_id'], 'status': item['status'],
                       'duration_seconds': item.get('execution_time'), 'failures': item.get('failures')}
                      for item in results],
            'association': 'Stored dbt invocation only; association with selected execution/warehouse is not proven.'}


def parity_status(path, data_dir):
    raw = read_json(path)
    checked = timestamp(raw['checked_at_utc'])
    if checked is None or type(raw.get('passed')) is not bool or not isinstance(raw.get('differences'), list):
        raise ValueError('Parity metadata missing or invalid')
    passed = raw['passed'] and not raw['differences']
    source = data_dir / 'processed/engineering.duckdb'
    expected = raw.get('source_sha256_after')
    matches = bool(source.is_file() and expected and hashlib.sha256(source.read_bytes()).hexdigest() == expected)
    return {'status': 'success' if passed else 'failed', 'source': str(path),
            'checked_at_utc': checked.isoformat(), 'repository_count': raw.get('repository_count'),
            'compared_column_count': raw.get('compared_column_count'), 'differences': raw['differences'],
            'matches_current_warehouse_bytes': matches,
            'source_unchanged_during_verification': expected is not None and raw.get('source_sha256_before') == expected,
            'association': 'Hash match identifies warehouse contents, not a pipeline run or current transformed database.'}


def build_report(data_dir, *, now=None, airflow_db=None, run_id=None,
                 docker_inspect=None, docker_result=None, docker_log=None,
                 dbt_results=None, parity_report=None):
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError('Monitoring clock must be timezone-aware')
    checked_at = checked_at.astimezone(timezone.utc)
    data_dir = Path(data_dir).resolve()
    errors = []

    def collect(name, operation):
        try:
            return operation()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, duckdb.Error) as error:
            errors.append({'source': name, 'error': type(error).__name__, 'message': str(error)})
            return {'status': 'unknown', 'error': 'evidence_unavailable'}

    warehouse = collect('warehouse', lambda: warehouse_status(data_dir, checked_at))
    executions = {}
    if airflow_db:
        executions['airflow'] = collect('airflow', lambda: airflow_status(airflow_db, run_id))
    if docker_inspect or docker_result or docker_log:
        executions['docker'] = collect('docker', lambda: docker_status(docker_inspect, docker_result, docker_log))
    validations = {}
    if dbt_results:
        validations['dbt'] = collect('dbt', lambda: dbt_status(dbt_results))
    if parity_report:
        validations['parity'] = collect('parity', lambda: parity_status(parity_report, data_dir))
    failures = [name for name, value in {**executions, **validations}.items() if value['status'] == 'failed']
    warnings = []
    extractions = []
    for result in warehouse.get('repositories', []):
        if result['status'] != 'ok':
            errors.append({'source': 'warehouse', 'error': result.get('error', 'quality_unavailable')})
            continue
        quality = result['data']
        extracted = timestamp(quality['latest_ingestion_run']['extracted_at_utc'])
        if extracted:
            extractions.append(extracted)
        warnings.extend(f"{result['repository']}: {warning}" for warning in quality['warnings'])
        if any(code in quality['warnings'] for code in ('source_ingestion_error', 'rejected_source_records', 'stored_quality_issues')):
            failures.append(f"warehouse:{result['repository']}")
    states = [value['status'] for value in executions.values()]
    execution_status = ('failed' if 'failed' in states else 'in_progress' if 'in_progress' in states
                        else 'success' if states and all(s == 'success' for s in states) else 'unknown')
    if warehouse['status'] != 'available':
        warnings.append('Warehouse metadata is unavailable or empty.')
    if set(validations) != {'dbt', 'parity'}:
        warnings.append('Both dbt and parity evidence were not supplied; no validation success is inferred.')
    for name, value in validations.items():
        if value['status'] == 'unknown':
            warnings.append(f'{name}: validation outcome is unknown.')
        recorded = timestamp(value.get('generated_at_utc', value.get('checked_at_utc')))
        if recorded and extractions and recorded < max(extractions):
            warnings.append(f'{name}: report predates the latest stored extraction; it cannot validate the current load.')
    docker = executions.get('docker', {})
    if docker.get('data_directory') and Path(docker['data_directory']).resolve() != data_dir:
        warnings.append('Docker result names a different data directory; its run and warehouse evidence are separate.')
    warnings.extend(docker.get('warnings', []))
    parity = validations.get('parity', {})
    if parity and (not parity.get('matches_current_warehouse_bytes') or not parity.get('source_unchanged_during_verification')):
        warnings.append('Parity report is not proven applicable to the selected current warehouse.')
    exit_code = 1 if failures or errors else 3 if execution_status != 'success' or warnings else 0
    return {'checked_at_utc': checked_at.isoformat(), 'data_directory': str(data_dir),
            'execution_status': execution_status, 'executions': executions, 'warehouse': warehouse,
            'validation_evidence': validations, 'observed_failures': failures,
            'evidence_errors': errors, 'warnings': warnings, 'exit_code': exit_code,
            'limitations': list(LIMITATIONS) + [
                'One-shot inspection, not polling, alerting, or pipeline execution.',
                'Selected execution and artifact sources are independent unless explicitly correlated; timestamps alone do not establish run identity.',
                'A passing historical report or fresh snapshot cannot establish pipeline success or override a failed execution.',
            ]}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--airflow-db', type=Path)
    parser.add_argument('--run-id')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--docker-inspect', type=Path)
    group.add_argument('--docker-result', type=Path)
    parser.add_argument('--docker-log', type=Path)
    parser.add_argument('--dbt-results', type=Path)
    parser.add_argument('--parity-report', type=Path)
    args = parser.parse_args(arguments)
    if bool(args.airflow_db) != bool(args.run_id):
        parser.error('--airflow-db and --run-id must be supplied together')
    report = build_report(**vars(args))
    directory = args.data_dir / 'reports/monitoring'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"pipeline_monitor_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}_{uuid4().hex[:8]}.json"
        with path.open('x', encoding='utf-8') as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write('\n')
    except (OSError, ValueError) as error:
        print(f'Monitoring report could not be written: {error}')
        return 1
    print(f"Execution: {report['execution_status']}; observed failures: {len(report['observed_failures'])}; "
          f"evidence errors: {len(report['evidence_errors'])}; warnings: {len(report['warnings'])}")
    for name, execution in report['executions'].items():
        print(f"  {name}: {execution['status']}; duration_seconds={execution.get('duration_seconds')}")
        for stage in execution.get('tasks', execution.get('stages', [])):
            print(f"    {stage.get('task_id', stage.get('stage'))}: {stage.get('state', stage.get('status'))}; "
                  f"duration_seconds={stage.get('duration_seconds')}")
    for warning in report['warnings']:
        print(f'  Warning: {warning}')
    for result in report['warehouse'].get('repositories', []):
        if result['status'] == 'ok':
            quality = result['data']
            latest = quality['latest_ingestion_run']
            print(f"  {result['repository']}: extracted_at_utc={latest['extracted_at_utc']}; "
                  f"age_hours={quality['freshness']['latest_extraction_age_hours']}; "
                  f"pages_fetched={latest['pages_fetched']}; pagination_complete={latest['pagination_complete']}; "
                  f"rejected={latest['rejected_records']}")
    for name, value in report['validation_evidence'].items():
        print(f"  Stored {name} evidence: {value['status']}")
    for failure in report['observed_failures']:
        print(f'  Failure: {failure}')
    for error in report['evidence_errors']:
        print(f'  Evidence error: {error}')
    print(f'Report: {path}')
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
