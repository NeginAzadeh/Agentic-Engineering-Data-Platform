# Engineering Analytics & Business KPIs

The parallel [dbt transformation layer](dbt_transformations.md) now materializes
documented analytical models in a separate database and checks parity against
these original SQL calculations. The existing reporting and Agent paths continue
using this SQL unchanged.

## Run and artifacts

From PowerShell in the project root, using the existing `.venv`:

```powershell
.\.venv\Scripts\python.exe .\src\engineering_intelligence\report_kpis.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The reporter opens `data/processed/engineering.duckdb` read-only. It requires the existing `pull_requests` and `ingestion_runs` tables; if the database is missing, run `load_github.py` first. No packages or network access are needed for reporting.

Each run writes a new `data/reports/engineering_kpis_<UTC timestamp>.json`. Reports are excluded from Git. Raw JSON, stored PRs, and ingestion functions are unchanged. The console shows repository summaries and the oldest five open PRs; JSON contains all observed open PR ages.

## Architecture

```text
DuckDB: pull_requests + ingestion_runs
  -> pr_observations.sql (temporary per-PR calculations)
  -> repository_kpis.sql + open_pr_ages.sql
  -> report_kpis.py (display and JSON export)
```

The temporary view exists only in the reporting connection. SQL owns the formulas, while Python executes queries and formats their results. Both report queries run in one transaction, reading a consistent database snapshot. Joining each PR to its supplying run gives provenance without multiplying PR rows. Repository metrics are aggregated before joining the one latest run per repository.

## Cohort and interpretation

Every report is labeled **observed, potentially incomplete PR cohort**. Its cohort consists of the newest stored observation of each repository/PR key, not just the latest snapshot's PRs. Previously observed PRs remain when absent from a later bounded extraction; their states and ages describe their own observation times. An observed open PR may have closed since extraction.

Pagination completeness means the extraction exhausted API pagination. It never establishes complete event history. The report includes latest-run metadata separately from contributing observation-run IDs, completeness, source errors, and the minimum/maximum PR extraction timestamps. If any contributing run is incomplete, combined completeness is false. Otherwise, missing/unknown provenance makes it null. It is true only if all contributing statuses are known and true; no observed PRs gives null.

Recent-first bounded extraction favors newer PRs and may omit older open work. Among recent PRs, slow merges may not have happened yet. Therefore the metrics cannot establish full backlog size, repository-wide merge performance, or causal explanations. Use them to identify questions for further investigation. Do not rank developer productivity or compare teams without additional context. No author-level metrics are calculated.

## Metric definitions

| Metric | Definition |
| --- | --- |
| Total observed PRs | Number of stored PR rows for a repository |
| Open PRs | Observed `state = 'open'` |
| Merged PRs | Observed `merged_at IS NOT NULL` |
| Closed without merge | Observed `state = 'closed'` and no merge date |
| Merge rate | Merged observed PRs divided by all observed PRs, including open PRs |
| Average/median time to merge | Creation-to-merge elapsed hours, among merged PRs with valid duration inputs |
| Open PR age | Creation-to-`source_extracted_at` elapsed days for each observed open PR |
| Average/median open age | Summary of valid per-PR open ages, potentially observed at different times |
| Open PRs older than 7 days | Valid open age strictly greater than 7 elapsed days (168 hours) |

The normal validated warehouse has mutually exclusive state groups. Durations retain fractional hours/days, not rounded calendar boundaries. Merge duration does not measure review effort or active working hours. Exactly seven days old is not older than seven days.

The report stores `merge_rate` as a fraction from 0 to 1; the console formats it as a percentage. Duration sample counts explain how many values entered each summary. Missing or negative duration inputs are excluded rather than converted to zero. Their PRs still contribute to observed counts. Per-PR ages and threshold flags are null when unavailable.

Zero observed PRs gives zero counts but null merge rate and durations. A nonempty cohort with no merged PRs gives a zero merge rate but null time-to-merge summaries. Repositories with ingestion metadata but no accepted PRs still appear, exposing failed/empty extractions. If both tables are empty, the report has an empty repositories list.

## SQL concepts, step by step

**Aggregation** summarizes many rows. `COUNT(*)` counts PR rows, `AVG(...)` computes an arithmetic mean, and `MEDIAN(...)` finds the middle value. `COUNT(duration)` counts only non-null durations, showing the sample size.

**GROUP BY** creates a separate group for each repository:

```sql
SELECT repository, COUNT(*) AS total_observed_prs
FROM analytics_pr_observations
GROUP BY repository;
```

**CASE WHEN** applies a condition to each row. Turning matching rows into 1 and other rows into 0 lets `SUM` count a category:

```sql
SUM(CASE WHEN state = 'open' THEN 1 ELSE 0 END) AS open_prs
```

For durations, a `CASE` without `ELSE` returns null when no condition matches. Aggregates such as `AVG` and `MEDIAN` ignore null inputs. Empty duration samples remain null, not zero. See [DuckDB aggregate functions](https://duckdb.org/docs/current/sql/functions/aggregates).

**Date calculations** use `epoch(timestamp)` to obtain seconds since the Unix epoch. Subtraction measures actual elapsed time; dividing by 3600 yields hours or by 86400 yields days:

```sql
(epoch(merged_at) - epoch(created_at)) / 3600.0
(epoch(source_extracted_at) - epoch(created_at)) / 86400.0
```

These calculations use absolute timestamp instants, including timezone offsets, rather than calendar-day boundaries. They never call `CURRENT_TIMESTAMP` or the system clock. The connection sets its timezone to UTC, and SQL formats exported timestamps with `Z`, avoiding optional Python timezone conversion packages. See [DuckDB timestamp functions](https://duckdb.org/docs/current/sql/functions/timestamptz).

**Median** sorts values and selects the middle one. With an even count, it averages the two central numeric values. For merge durations `[2, 4, 18]` hours, the median is 4 and the mean is 8. Median is less affected by a few unusually long durations; neither statistic corrects cohort selection bias.

**NULLIF** turns a zero denominator into null, avoiding division by zero. Multiplication by `1.0` makes the intended fraction explicit:

```sql
merged_prs * 1.0 / NULLIF(total_observed_prs, 0)
```

**COALESCE** substitutes a default where appropriate. Missing counts become zero; unavailable rates and durations remain null to distinguish no evidence from a measured zero.

**CTEs and joins** organize the repository query. `WITH pr_metrics AS (...)` names the grouped PR calculation. `ROW_NUMBER()` selects one latest ingestion run per repository, breaking extraction-time ties by run ID. A repository union includes run-only repositories, and left joins keep them visible. Joining every PR directly to every repository run would inflate counts; this query avoids that mistake.

## JSON contract

The report includes:

- `report_version`, `cohort_label`, `intended_use`, and `metric_definitions`.
- `generated_at_utc`, used only to identify the exported artifact.
- A `repositories` list with repository identity, cohort/provenance context, numeric `metrics`, and all per-PR `open_prs` ages.
- Latest ingestion errors, accepted/rejected counts, and quality issue details.

Generated timestamps and filenames can differ across runs; KPI values remain reproducible for an unchanged warehouse. JSON null means unavailable, never NaN or infinity. Full numeric precision is retained in JSON; display rounding affects only console output. This artifact is prepared for future consumers, but no AI Agent is implemented.

## Validation

Automated tests use isolated controlled DuckDB datasets. They check known counts/rates, odd and even medians, fractional time calculations, the seven-day boundary, multiple repositories and ingestion runs, failed latest extractions, mixed observation times, missing provenance, null/invalid durations, empty datasets, deterministic calculations without the clock, JSON export, and preservation of a read-only database.

No dbt models, dashboard, cloud services, AI Agent, or developer ranking is introduced by this milestone.
