"""Controlled files, clocks, and read-only databases; no pipeline execution."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb

from orchestration.pipeline_runtime import STAGES
from src.engineering_intelligence import monitor_pipeline as monitor
from src.engineering_intelligence.load_github import create_tables


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        (self.data / 'processed').mkdir(parents=True)
        self.warehouse = self.data / 'processed/engineering.duckdb'
        self.now = datetime(2026, 10, 10, 1, tzinfo=timezone.utc)
        with duckdb.connect(str(self.warehouse)) as c:
            create_tables(c)
            c.execute("""INSERT INTO ingestion_runs VALUES
                ('run-a','example/repo','2026-10-10T00:00:00Z',NULL,
                 'https://api.github.com?page=1','fixture.json',TRUE,3,'exhausted',NULL,1,1,0,'[]')""")
            c.execute("""INSERT INTO pull_requests VALUES
                ('example/repo',1,'Fixture PR','open','author','2026-10-01T00:00:00Z',
                 '2026-10-10T00:00:00Z',NULL,NULL,'2026-10-10T00:00:00Z','run-a')""")
        self.airflow = self.root / 'airflow.db'
        c = sqlite3.connect(self.airflow)
        try:
            c.executescript('''CREATE TABLE dag_run (dag_id TEXT,run_id TEXT,state TEXT,start_date TEXT,end_date TEXT);
                CREATE TABLE task_instance (dag_id TEXT,run_id TEXT,task_id TEXT,state TEXT,start_date TEXT,end_date TEXT,try_number INTEGER);''')
            c.execute('INSERT INTO dag_run VALUES (?,?,?,?,?)', [monitor.DAG_ID, 'manual-test', 'success',
                      '2026-10-10 00:10:00', '2026-10-10 00:10:20'])
            for index, stage in enumerate(STAGES):
                c.execute('INSERT INTO task_instance VALUES (?,?,?,?,?,?,?)',
                          [monitor.DAG_ID, 'manual-test', stage, 'success',
                           f'2026-10-10 00:10:{index * 5:02d}', f'2026-10-10 00:10:{index * 5 + 4:02d}', 1])
            c.commit()
        finally:
            c.close()
        self.dbt = self.write('run_results.json', {
            'metadata': {'generated_at': '2026-10-10T00:20:00Z', 'invocation_id': 'fixture'},
            'results': [{'unique_id': 'model.fixture', 'status': 'success', 'execution_time': 2.5},
                        {'unique_id': 'test.fixture', 'status': 'pass', 'execution_time': 0.25, 'failures': 0}]})
        self.parity = self.write_parity()

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def write_parity(self, passed=True):
        digest = hashlib.sha256(self.warehouse.read_bytes()).hexdigest()
        return self.write('parity.json', {'checked_at_utc': '2026-10-10T00:30:00Z',
                         'passed': passed, 'differences': [] if passed else [{'column': 'open_prs'}],
                         'repository_count': 1, 'compared_column_count': 26,
                         'source_sha256_before': digest, 'source_sha256_after': digest})

    def report(self, **kwargs):
        options = dict(now=self.now, airflow_db=self.airflow, run_id='manual-test',
                       dbt_results=self.dbt, parity_report=self.parity)
        options.update(kwargs)
        return monitor.build_report(self.data, **options)

    def update_airflow(self, sql, values=()):
        c = sqlite3.connect(self.airflow)
        try:
            c.execute(sql, values)
            c.commit()
        finally:
            c.close()

    def docker(self, code=0, state='exited', timestamped=True, partial=False):
        inspect = self.write('docker.json', [{'Id': 'fixture-container',
            'Config': {'Env': ['SECRET_VALUE=must-never-appear']},
            'State': {'Status': state, 'ExitCode': code, 'OOMKilled': False, 'Error': '',
                      'StartedAt': '2026-10-10T00:10:00.000000001Z',
                      'FinishedAt': '2026-10-10T00:10:20.000000001Z'}}])
        lines = []
        for index, stage in enumerate(STAGES[:2] if partial else STAGES):
            prefix = f'2026-10-10T00:10:{index * 5:02d}Z ' if timestamped else ''
            lines.append(prefix + f'Starting {stage}')
            if not partial or index == 0:
                prefix = f'2026-10-10T00:10:{index * 5 + 4:02d}Z ' if timestamped else ''
                lines.append(prefix + f'Completed {stage}')
        log = self.root / 'docker.log'
        log.write_text('\n'.join(lines), encoding='utf-8')
        return inspect, log

    def test_successful_airflow_read_has_real_durations_and_does_not_modify_inputs(self):
        paths = [self.warehouse, self.airflow, self.dbt, self.parity]
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            result = self.report()
        self.assertEqual(result['execution_status'], 'success')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['executions']['airflow']['duration_seconds'], 20)
        self.assertTrue(all(t['duration_seconds'] == 4 for t in result['executions']['airflow']['tasks']))
        self.assertEqual(result['warehouse']['repositories'][0]['data']['freshness']['latest_extraction_age_hours'], 1)
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_every_warehouse_connection_is_read_only(self):
        original = duckdb.connect
        with patch.object(monitor.duckdb, 'connect', wraps=original) as connections:
            self.report()
        self.assertTrue(connections.call_args_list)
        self.assertTrue(all(call.kwargs.get('read_only') is True for call in connections.call_args_list))

    def test_failed_and_upstream_failed_tasks_cannot_be_hidden_by_passing_reports(self):
        self.update_airflow("UPDATE dag_run SET state='failed'")
        self.update_airflow("UPDATE task_instance SET state='failed' WHERE task_id='load_warehouse'")
        self.update_airflow("UPDATE task_instance SET state='upstream_failed',start_date=NULL,end_date=NULL WHERE task_id IN ('dbt_build','verify_kpis')")
        result = self.report()
        self.assertEqual(result['execution_status'], 'failed')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['validation_evidence']['parity']['status'], 'success')
        self.assertTrue(any(t['state'] == 'upstream_failed' and t['duration_seconds'] is None
                            for t in result['executions']['airflow']['tasks']))

    def test_successful_run_row_with_failed_task_is_still_failed(self):
        self.update_airflow("UPDATE task_instance SET state='failed' WHERE task_id='verify_kpis'")
        self.assertEqual(self.report()['execution_status'], 'failed')

    def test_missing_task_or_running_execution_never_reports_success(self):
        self.update_airflow("DELETE FROM task_instance WHERE task_id='verify_kpis'")
        self.assertEqual(self.report()['execution_status'], 'unknown')
        self.assertEqual(self.report()['exit_code'], 3)
        self.update_airflow("UPDATE dag_run SET state='running',end_date=NULL")
        result = self.report()
        self.assertEqual(result['execution_status'], 'in_progress')
        self.assertIsNone(result['executions']['airflow']['duration_seconds'])

    def test_missing_or_quoted_run_id_is_not_a_fallback_to_another_success(self):
        result = self.report(run_id="missing' OR 1=1 --")
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['evidence_errors'][0]['source'], 'airflow')

    def test_stale_observation_is_separate_from_old_pr_age_and_known_page_count(self):
        with duckdb.connect(str(self.warehouse)) as c:
            c.execute("UPDATE ingestion_runs SET pagination_complete=FALSE, stop_reason='page_limit'")
        result = self.report(now=self.now + timedelta(days=2))
        quality = result['warehouse']['repositories'][0]['data']
        self.assertEqual(quality['freshness']['latest_extraction_age_hours'], 49)
        self.assertTrue(quality['freshness']['latest_extraction_is_stale'])
        self.assertEqual(quality['latest_ingestion_run']['pages_fetched'], 3)
        self.assertFalse(quality['latest_ingestion_run']['pagination_complete'])
        self.assertEqual(result['execution_status'], 'success')
        self.assertEqual(result['exit_code'], 3)

    def test_partial_source_failure_and_rejections_are_visible_with_successful_execution(self):
        with duckdb.connect(str(self.warehouse)) as c:
            c.execute("UPDATE ingestion_runs SET source_error='timeout',rejected_records=1,quality_issues='[\"bad row\"]'")
        result = self.report()
        self.assertEqual(result['exit_code'], 1)
        self.assertIn('warehouse:example/repo', result['observed_failures'])
        self.assertIn('example/repo: source_ingestion_error', result['warnings'])

    def test_missing_metadata_stays_unknown_not_fresh_or_complete(self):
        with duckdb.connect(str(self.warehouse)) as c:
            c.execute('DELETE FROM ingestion_runs')
        result = self.report()
        quality = result['warehouse']['repositories'][0]['data']
        self.assertIsNone(quality['freshness']['latest_extraction_age_hours'])
        self.assertIsNone(quality['latest_ingestion_run']['pagination_complete'])
        self.assertIn('missing_ingestion_metadata', quality['warnings'])
        self.assertNotEqual(result['exit_code'], 0)

    def test_missing_databases_are_not_created(self):
        missing = self.root / 'missing/airflow.db'
        result = self.report(airflow_db=missing)
        self.assertEqual(result['exit_code'], 1)
        self.assertFalse(missing.exists())
        data = self.root / 'absent-data'
        result = monitor.build_report(data, now=self.now)
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(data.exists())

    def test_docker_success_durations_and_secret_exclusion(self):
        inspect, log = self.docker()
        result = monitor.docker_status(inspect_path=inspect, log_path=log)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['duration_seconds'], 20)
        self.assertTrue(all(t['duration_seconds'] == 4 for t in result['stages']))
        self.assertNotIn('SECRET_VALUE', json.dumps(result))

    def test_docker_nonzero_exit_with_all_completions_remains_failed(self):
        inspect, log = self.docker(code=7)
        result = self.report(airflow_db=None, run_id=None, docker_inspect=inspect, docker_log=log)
        self.assertEqual(result['execution_status'], 'failed')
        self.assertEqual(result['exit_code'], 1)

    def test_partial_docker_failure_and_untimestamped_logs_do_not_invent_durations(self):
        inspect, log = self.docker(code=1, timestamped=False, partial=True)
        result = monitor.docker_status(inspect_path=inspect, log_path=log)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stages'][1]['status'], 'failed_or_interrupted')
        self.assertEqual(result['stages'][2]['status'], 'not_observed')
        self.assertTrue(all(t['duration_seconds'] is None for t in result['stages']))

    def test_docker_log_failure_without_container_state_is_not_success(self):
        _, log = self.docker(timestamped=False, partial=True)
        with log.open('a') as output:
            output.write('\nPipeline timed out: fixture timeout\n')
        self.assertEqual(monitor.docker_status(log_path=log)['status'], 'failed')

    def test_removed_docker_container_result_reuses_recorded_exit_and_duration(self):
        _, log = self.docker(timestamped=False)
        record = self.write('live-results.json', {'compose_exit': 0, 'elapsed_seconds': 15.1,
                                                'data_directory': str(self.data)})
        result = monitor.docker_status(result_path=record, log_path=log)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['duration_seconds'], 15.1)
        self.assertTrue(all(t['duration_seconds'] is None for t in result['stages']))

    def test_exit_zero_without_stage_evidence_is_not_a_complete_pipeline(self):
        inspect, _ = self.docker()
        self.assertEqual(monitor.docker_status(inspect_path=inspect)['status'], 'unknown')

    def test_docker_oom_cannot_pass_even_with_exit_zero_and_completion_logs(self):
        inspect, log = self.docker()
        raw = json.loads(inspect.read_text())
        raw[0]['State']['OOMKilled'] = True
        inspect.write_text(json.dumps(raw))
        self.assertEqual(monitor.docker_status(inspect_path=inspect, log_path=log)['status'], 'failed')

    def test_failed_dbt_or_parity_stays_visible(self):
        raw = json.loads(self.dbt.read_text())
        raw['results'][1]['status'] = 'fail'
        self.dbt.write_text(json.dumps(raw))
        self.write_parity(False)
        result = self.report()
        self.assertEqual(set(result['observed_failures']), {'dbt', 'parity'})
        self.assertEqual(result['exit_code'], 1)

    def test_old_and_mismatched_reports_are_not_claimed_current(self):
        raw = json.loads(self.dbt.read_text())
        raw['metadata']['generated_at'] = '2026-10-09T00:00:00Z'
        self.dbt.write_text(json.dumps(raw))
        raw = json.loads(self.parity.read_text())
        raw['source_sha256_after'] = 'wrong-warehouse'
        self.parity.write_text(json.dumps(raw))
        result = self.report()
        self.assertEqual(result['exit_code'], 3)
        self.assertTrue(any('predates' in item for item in result['warnings']))
        self.assertFalse(result['validation_evidence']['parity']['matches_current_warehouse_bytes'])

    def test_invalid_metadata_reports_error_instead_of_falling_back(self):
        self.dbt.write_text('{invalid-json')
        result = self.report()
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['validation_evidence']['dbt']['status'], 'unknown')
        self.assertTrue(result['evidence_errors'])

    def test_cli_exports_new_reports_and_preserves_failed_status(self):
        self.update_airflow("UPDATE dag_run SET state='failed'")
        args = ['--data-dir', str(self.data), '--airflow-db', str(self.airflow), '--run-id', 'manual-test']
        with redirect_stdout(StringIO()):
            self.assertEqual(monitor.main(args), 1)
            self.assertEqual(monitor.main(args), 1)
        paths = list((self.data / 'reports/monitoring').glob('*.json'))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(json.loads(p.read_text())['execution_status'] == 'failed' for p in paths))

    def test_cli_requires_explicit_airflow_run_and_never_creates_missing_db(self):
        missing = self.root / 'missing.db'
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as result:
            monitor.main(['--airflow-db', str(missing)])
        self.assertEqual(result.exception.code, 2)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
