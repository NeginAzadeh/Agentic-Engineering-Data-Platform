"""Real local dbt builds against isolated, controlled DuckDB warehouses."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

from src.engineering_intelligence.load_github import create_tables
from src.engineering_intelligence.verify_dbt_kpis import compare_kpis, query_rows


ROOT = Path(__file__).resolve().parents[1]
DBT = Path(sys.executable).with_name('dbt.exe' if os.name == 'nt' else 'dbt')


@unittest.skipUnless(DBT.is_file(), 'Install requirements-dbt.txt to run real dbt integration tests')
class DbtIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        processed = cls.root / 'data/processed'
        processed.mkdir(parents=True)
        cls.source = processed / 'engineering.duckdb'
        cls.output = processed / 'engineering_dbt.duckdb'
        with duckdb.connect(str(cls.source)) as c:
            create_tables(c)
            for run_id, repo, extracted, complete, error in [
                ('old', 'example/alpha', '2026-10-03', False, 'timeout'),
                ('a-new', 'example/alpha', '2026-10-20', True, None),
                ('z-tie', 'example/alpha', '2026-10-20', True, None),
                ('beta', 'example/beta', '2026-10-10', None, None),
                ('empty', 'example/empty', '2026-10-10', False, None),
                ('unmerged', 'example/unmerged', '2026-10-10', True, None),
            ]:
                c.execute('''INSERT INTO ingestion_runs VALUES
                    (?, ?, ?, NULL, 'https://api.github.com?page=1', 'fixture.json',
                     ?, ?, ?, ?, 0, 0, 0, '[]')''',
                    [run_id, repo, extracted + 'T00:00:00Z', complete,
                     None if complete is None else 3,
                     'partial_failure' if error else ('exhausted' if complete else 'page_limit'), error])

            def pr(number, repo='example/alpha', run='a-new', created='2026-10-01T00:00:00Z',
                   observed='2026-10-20T00:00:00Z', state='open', merged=None):
                c.execute('''INSERT INTO pull_requests VALUES
                    (?, ?, 'Fixture PR', ?, 'developer', ?, ?, ?, ?, ?, ?)''',
                    [repo, number, state, created, merged or observed,
                     (merged or observed) if state == 'closed' else None, merged, observed, run])

            pr(1, run='old', observed='2026-10-03T00:00:00Z')  # age 2, not 19
            pr(2, created='2026-10-13T00:00:00Z')  # exactly 7, excluded from >7
            pr(3, created='2026-10-11T00:00:00Z')  # age 9
            for number, hour in [(4, 2), (5, 4), (6, 18)]:
                pr(number, state='closed', merged=f'2026-10-01T{hour:02d}:00:00Z')
            pr(7, state='closed')
            for number, hour in [(1, '00'), (2, '03')]:
                pr(number, repo='example/beta', run='beta', state='closed',
                   observed='2026-10-10T00:00:00Z', merged=f'2026-10-01T{hour}:30:00Z')
            # Offset input is a UTC instant, not local-clock arithmetic.
            pr(1, repo='example/unmerged', run='unmerged', created='2026-10-03T02:00:00+02:00',
               observed='2026-10-10T00:00:00Z')
            c.execute('''UPDATE ingestion_runs SET record_count = (
                SELECT count(*) FROM pull_requests p WHERE p.last_run_id = ingestion_runs.run_id)''')
            c.execute('UPDATE ingestion_runs SET accepted_records = record_count')
        cls.source_hash = hashlib.sha256(cls.source.read_bytes()).hexdigest()
        cls.build = cls.run_dbt('build')
        if cls.build.returncode:
            raise AssertionError(cls.build.stdout)
        cls.initial_results = json.loads((cls.root / 'target/run_results.json').read_text())

    @classmethod
    def run_dbt(cls, command, *options, project_root=None):
        project_root = project_root or cls.root
        env = dict(os.environ, ENGINEERING_PROJECT_ROOT=str(project_root),
                   DBT_SEND_ANONYMOUS_USAGE_STATS='false')
        result = subprocess.run(
            [str(DBT), '--no-use-colors', '--log-path', str(project_root / 'logs'), command,
             '--project-dir', str(ROOT / 'dbt'), '--profiles-dir', str(ROOT / 'dbt'),
             '--target-path', str(project_root / 'target'), *options],
            env=env, cwd=ROOT, text=True, encoding='utf-8', errors='replace',
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
        )
        return result

    def mart_rows(self):
        with duckdb.connect(str(self.output), read_only=True) as c:
            _, rows = query_rows(c, 'select * from analytics_marts.mart_repository_kpis')
        return {row['repository']: row for row in rows}

    def test_real_build_all_data_tests_and_source_preservation(self):
        self.assertTrue(all(item['status'] in ('success', 'pass') for item in self.initial_results['results']))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_all_legacy_columns_match_independent_existing_sql(self):
        result = compare_kpis(self.source, self.output)
        self.assertTrue(result['passed'], result['differences'])
        self.assertEqual(result['repository_count'], 4)

    def test_known_counts_merge_rate_mean_median_and_strict_age_boundary(self):
        row = self.mart_rows()['example/alpha']
        self.assertEqual([row[k] for k in ('total_observed_prs', 'open_prs', 'merged_prs',
                                         'closed_without_merge_prs')], [7, 3, 3, 1])
        self.assertAlmostEqual(row['merge_rate'], 3 / 7)
        self.assertEqual(row['average_time_to_merge_hours'], 8)
        self.assertEqual(row['median_time_to_merge_hours'], 4)
        self.assertEqual(row['average_open_age_days'], 6)
        self.assertEqual(row['median_open_age_days'], 7)
        self.assertEqual(row['open_prs_older_than_7_days'], 1)
        self.assertEqual(self.mart_rows()['example/beta']['median_time_to_merge_hours'], 2)

    def test_empty_cohort_and_missing_durations_remain_null(self):
        rows = self.mart_rows()
        self.assertEqual(rows['example/empty']['total_observed_prs'], 0)
        self.assertIsNone(rows['example/empty']['merge_rate'])
        self.assertIsNone(rows['example/empty']['average_open_age_days'])
        self.assertIsNone(rows['example/empty']['all_observation_runs_pagination_complete'])
        self.assertEqual(rows['example/unmerged']['merge_rate'], 0)
        self.assertIsNone(rows['example/unmerged']['median_time_to_merge_hours'])
        self.assertEqual(rows['example/unmerged']['average_open_age_days'], 7)

    def test_contributing_runs_do_not_inherit_latest_run_completeness(self):
        rows = self.mart_rows()
        self.assertTrue(rows['example/alpha']['latest_run_pagination_complete'])
        self.assertFalse(rows['example/alpha']['all_observation_runs_pagination_complete'])
        self.assertEqual(rows['example/alpha']['prs_from_runs_with_source_errors'], 1)
        self.assertEqual(rows['example/alpha']['latest_run_id'], 'a-new')
        self.assertIsNone(rows['example/beta']['all_observation_runs_pagination_complete'])

    def test_parity_detects_real_discrepancy_without_changing_source(self):
        # Only mutate the disposable transformed table, then restore it.
        with duckdb.connect(str(self.output)) as c:
            original = c.execute("select open_prs from analytics_marts.mart_repository_kpis where repository = 'example/alpha'").fetchone()[0]
            c.execute("update analytics_marts.mart_repository_kpis set open_prs = 999 where repository = 'example/alpha'")
        try:
            result = compare_kpis(self.source, self.output)
            self.assertFalse(result['passed'])
            self.assertTrue(any(d.get('column') == 'open_prs' for d in result['differences']))
        finally:
            with duckdb.connect(str(self.output)) as c:
                c.execute("update analytics_marts.mart_repository_kpis set open_prs = ? where repository = 'example/alpha'", [original])

    def test_rebuild_is_repeatable_and_does_not_duplicate_facts(self):
        before = self.mart_rows()
        result = self.run_dbt('run')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mart_rows(), before)
        with duckdb.connect(str(self.output), read_only=True) as c:
            self.assertEqual(c.execute('select count(*) from analytics_marts.fct_pull_requests').fetchone()[0], 10)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_custom_lifecycle_test_rejects_bad_facts(self):
        with duckdb.connect(str(self.output)) as c:
            c.execute('create table analytics_marts.fact_backup as select * from analytics_marts.fct_pull_requests')
            c.execute("""update analytics_marts.fct_pull_requests
                set merged_at_utc = created_at_utc - interval '1 hour'
                where repository = 'example/alpha' and pr_number = 4""")
        try:
            result = self.run_dbt('test', '--select', 'assert_pr_lifecycle')
            self.assertEqual(result.returncode, 1, result.stdout)
            artifact = json.loads((self.root / 'target/run_results.json').read_text())
            self.assertEqual(artifact['results'][0]['status'], 'fail')
            self.assertEqual(artifact['results'][0]['failures'], 1)
        finally:
            with duckdb.connect(str(self.output)) as c:
                c.execute('delete from analytics_marts.fct_pull_requests')
                c.execute('insert into analytics_marts.fct_pull_requests select * from analytics_marts.fact_backup')
                c.execute('drop table analytics_marts.fact_backup')

    def test_relationship_test_checks_repository_even_when_run_id_exists(self):
        view = 'analytics_staging.stg_github__pull_requests'
        with duckdb.connect(str(self.output)) as c:
            original = c.execute("select sql from duckdb_views() where schema_name = 'analytics_staging' and view_name = 'stg_github__pull_requests'").fetchone()[0]
            # A real run ID from the wrong repository must not satisfy provenance.
            c.execute(f'''create or replace view {view} as
                select * replace (case when repository = 'example/alpha' and pr_number = 1
                    then 'beta' else observation_run_id end as observation_run_id)
                from analytics_marts.fct_pull_requests''')
        try:
            result = self.run_dbt('test', '--select', 'assert_pr_observation_relationship')
            self.assertEqual(result.returncode, 1, result.stdout)
            artifact = json.loads((self.root / 'target/run_results.json').read_text())
            self.assertEqual(artifact['results'][0]['status'], 'fail')
            self.assertEqual(artifact['results'][0]['failures'], 1)
        finally:
            with duckdb.connect(str(self.output)) as c:
                # ATTACH is DDL and does not accept bound parameters. Escape the
                # test-owned temporary path; no model/user input is involved.
                source_literal = str(self.source).replace("'", "''")
                c.execute(f"ATTACH '{source_literal}' AS engineering_source (READ_ONLY)")
                c.execute(original.replace('CREATE VIEW', 'CREATE OR REPLACE VIEW', 1))

    def test_completely_empty_warehouse_builds_with_no_fabricated_repositories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processed = root / 'data/processed'
            processed.mkdir(parents=True)
            source = processed / 'engineering.duckdb'
            output = processed / 'engineering_dbt.duckdb'
            with duckdb.connect(str(source)) as c:
                create_tables(c)
            result = self.run_dbt('build', project_root=root)
            self.assertEqual(result.returncode, 0, result.stdout)
            report = compare_kpis(source, output)
            self.assertTrue(report['passed'], report['differences'])
            self.assertEqual(report['repository_count'], 0)


class DbtParityInputTests(unittest.TestCase):
    def test_missing_input_does_not_create_any_database(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'missing.duckdb'
            with self.assertRaises(FileNotFoundError):
                compare_kpis(source, Path(temp) / 'target.duckdb')
            self.assertFalse(source.exists())


if __name__ == '__main__':
    unittest.main()
