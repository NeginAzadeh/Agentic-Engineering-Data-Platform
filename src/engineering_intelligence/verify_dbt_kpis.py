"""Compare dbt's KPI table with the unchanged, established analytics SQL."""

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYTICS_DIR = PROJECT_ROOT / 'sql' / 'analytics'


def query_rows(connection, sql):
    cursor = connection.execute(sql)
    columns = [column[0] for column in cursor.description]
    return columns, [dict(zip(columns, row)) for row in cursor.fetchall()]


def compare_kpis(source_path, transformed_path):
    """Open both files read-only; compare all legacy columns, including provenance."""
    source_path, transformed_path = Path(source_path), Path(transformed_path)
    # DuckDB otherwise creates a missing database in read/write mode; require real files.
    for path in (source_path, transformed_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with duckdb.connect(str(source_path), read_only=True) as source:
        source.execute("SET TimeZone = 'UTC'")
        source.execute('BEGIN TRANSACTION')
        source.execute((ANALYTICS_DIR / 'pr_observations.sql').read_text())
        columns, baseline = query_rows(source, (ANALYTICS_DIR / 'repository_kpis.sql').read_text())
        source.execute('COMMIT')
    with duckdb.connect(str(transformed_path), read_only=True) as transformed:
        actual_columns, actual = query_rows(
            transformed, 'SELECT * FROM analytics_marts.mart_repository_kpis ORDER BY repository'
        )
    differences = []
    for column in columns:
        if column not in actual_columns:
            differences.append({'column': column, 'issue': 'missing column'})
    expected_by_repo = {row['repository']: row for row in baseline}
    actual_by_repo = {row['repository']: row for row in actual}
    if len(actual_by_repo) != len(actual):
        differences.append({'issue': 'duplicate repository rows in dbt mart'})
    for repository in sorted(expected_by_repo.keys() | actual_by_repo.keys()):
        if repository not in expected_by_repo or repository not in actual_by_repo:
            differences.append({'repository': repository, 'issue': 'repository set differs'})
            continue
        for column in columns:
            expected = expected_by_repo[repository][column]
            observed = actual_by_repo[repository].get(column)
            equal = expected == observed
            if isinstance(expected, float) and isinstance(observed, (float, int)):
                equal = math.isclose(expected, observed, rel_tol=1e-12, abs_tol=1e-9)
            if not equal:
                differences.append({'repository': repository, 'column': column,
                                    'expected': expected, 'actual': observed})
    after = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if before != after:
        differences.append({'issue': 'source file changed during verification'})
    return {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_database': str(source_path.resolve()),
        'transformed_database': str(transformed_path.resolve()),
        'source_sha256_before': before, 'source_sha256_after': after,
        'repository_count': len(baseline), 'compared_column_count': len(columns),
        'cohort_scope': 'observed, potentially incomplete PR cohort',
        'passed': not differences, 'differences': differences,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=PROJECT_ROOT / 'data/processed/engineering.duckdb')
    parser.add_argument('--transformed', type=Path, default=PROJECT_ROOT / 'data/processed/engineering_dbt.duckdb')
    args = parser.parse_args()
    try:
        report = compare_kpis(args.source, args.transformed)
    except (OSError, duckdb.Error) as error:
        print(f'Verification failed: {error}')
        return 1
    report_dir = PROJECT_ROOT / 'data/reports'
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report_path = report_dir / f'dbt_kpi_parity_{stamp}.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f"KPI parity: {'PASS' if report['passed'] else 'FAIL'}; "
          f"{report['repository_count']} repositories, {report['compared_column_count']} columns, "
          f"{len(report['differences'])} discrepancies.")
    for difference in report['differences']:
        print(json.dumps(difference))
    print(f'Report: {report_path}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
