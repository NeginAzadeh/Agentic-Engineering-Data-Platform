# Local pipeline monitoring and observability

## Implemented scope

`monitor_pipeline.py` performs one inspection of existing local evidence. It
does not run ingestion, dbt, Airflow tasks, Docker containers, or Ollama, and it
does not poll. It uses the existing Python/DuckDB environment and adds no service
or dependency. Airflow and Docker remain responsible for executing the pipeline
and recording logs; the monitor brings their available evidence into one JSON
report and console summary.

Execution success and data health are separate. A successful bounded extraction
can leave incomplete pagination or older retained observations. Conversely, an
old passing validation report cannot erase a failed task or failed extraction.
Missing evidence stays unknown rather than becoming success, zero, or fresh data.

## Evidence and boundaries

| Source | What the monitor reads | What it establishes |
| --- | --- | --- |
| Existing standalone Airflow SQLite metadata | An explicitly selected DAG run and its four task instances, states, start/end times, and try numbers | Stored run/task outcomes and elapsed seconds; inspect the existing UI/logs for each attempt's traceback |
| Docker exports | One container's `docker inspect` JSON and existing runner logs, or the Docker milestone's saved `live-results.json` | Container/Compose exit outcome, elapsed time when present, and observed stage markers |
| Source DuckDB warehouse | `ingestion_runs` and contributing PR observations, via the existing read-only `AgentTools.get_data_quality_status` | Extraction timestamps, freshness, page counts/completeness, ingestion errors, rejected records, and stored quality warnings |
| dbt `run_results.json` | Invocation ID, generation time, node outcomes, execution times, and failure counts | Outcome of that stored invocation; no models or tests are rerun |
| KPI parity JSON | Stored outcome, differences, timestamp, repository/column counts, and source hashes | Stored parity result and whether source bytes match the selected warehouse today |

Airflow SQLite is opened with `mode=ro` and query-only access. Its DAG/run lookup
uses bound parameters and the fixed `engineering_data_pipeline` DAG ID. DuckDB
connections are read-only. No SQL, shell, Python, or model execution is accepted
as monitoring input. The monitor deliberately excludes Docker `Config.Env` from
its report; raw exports can still contain secrets, so keep them local and ignored.

Supply exact artifact paths. The monitor does not select an older passing report
when the requested evidence is missing or malformed. Run metadata and validation
artifacts are independent unless their provenance explicitly ties them together.
A report older than the latest extraction is flagged. A parity source-hash match
identifies warehouse bytes, not an Airflow run or the present transformed database.
dbt's mutable `target/run_results.json` can be replaced by a later command.

## Inspect from Windows PowerShell

Run from the project root, using the existing Windows `.venv`:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.monitor_pipeline --data-dir .\data
$LASTEXITCODE
```

This inspects stored data health only. Execution is `unknown` because no execution
source was supplied. A new report is written under
`data/reports/monitoring/pipeline_monitor_<UTC>_<unique suffix>.json`.
Existing `data/reports/` ignore rules cover it; no generated evidence is tracked.
Every invocation creates a new file rather than overwriting historical reports.

Add existing validation evidence when available (replace the parity filename):

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.monitor_pipeline `
  --data-dir .\data `
  --dbt-results .\dbt\target\run_results.json `
  --parity-report .\data\reports\dbt_kpi_parity_<timestamp>.json
```

This still does not infer a pipeline execution from validation artifacts alone.
The JSON includes `execution_status`, `executions`, `warehouse`,
`validation_evidence`, `observed_failures`, `evidence_errors`, `warnings`, and
`limitations`. The warehouse section retains latest and contributing ingestion
metadata, not just a flattened green/red result.

## Inspect a recorded Airflow run in WSL2

Use Linux paths and the existing **pipeline** venv, which contains DuckDB. The
Airflow venv is not required by the monitor. For the historical validated run:

```bash
cd /mnt/c/Users/Negin/Documents/ai-data-quality-platform
/home/negin/.venvs/aedp-pipeline/bin/python -m src.engineering_intelligence.monitor_pipeline \
  --data-dir "$PWD/data" \
  --airflow-db /home/negin/.local/share/aedp-airflow/airflow.db \
  --run-id 'manual__2026-11-23T09:21:18.097597+00:00' \
  --dbt-results "$PWD/dbt/target/run_results.json" \
  --parity-report "$PWD/data/reports/dbt_kpi_parity_20261123T092150851572Z.json"
echo "$?"
```

Choose another exact run ID to inspect another execution; there is no implicit
latest-success fallback. Both `--airflow-db` and `--run-id` are required together.
The adapter targets the validated Airflow 3.3.2 standalone SQLite schema. It does
not connect to a remote Airflow server or support other metadata backends.
Stored task durations cover the recorded task instance; prior attempts and
detailed exceptions remain in Airflow's existing UI/task logs. See
[the Airflow guide](airflow_orchestration.md) for UI access and log locations.

## Inspect Docker evidence

The monitor is a host CLI, not a new container service or image component. It
does not contact the Docker daemon. For an existing retained container, export
its evidence to an ignored location, using your existing Docker permissions:

```bash
mkdir -p data/reports/docker-evidence
docker inspect <container-id> > data/reports/docker-evidence/inspect.json
docker logs --timestamps <container-id> > data/reports/docker-evidence/log.txt 2>&1
/home/negin/.venvs/aedp-pipeline/bin/python -m src.engineering_intelligence.monitor_pipeline \
  --data-dir /absolute/host/directory/mounted/as/pipeline-data \
  --docker-inspect "$PWD/data/reports/docker-evidence/inspect.json" \
  --docker-log "$PWD/data/reports/docker-evidence/log.txt" \
  --dbt-results /absolute/host/directory/mounted/as/pipeline-data/dbt/target/run_results.json \
  --parity-report /absolute/path/to/the/recorded/parity-report.json
```

Do not type angle-bracket placeholders literally. Select the same container's
logs and its actual host data mount. Docker output is read as evidence, never as
instructions. A zero exit alone does not prove all four stages completed; their
existing `Starting`/`Completed` markers must also be present. Nonzero exit, OOM,
container errors, and runner failure markers remain failures even with completion
markers or passing dbt/parity reports. Unobserved downstream stages are labeled
`not_observed`; the monitor does not invent their outcomes.

Timestamped start/end markers provide stage elapsed seconds. Legacy logs without
timestamps retain outcomes but leave durations `null`. Export one execution per
file; repeated markers are rejected. Log/container association is caller-selected
and always carries a warning, not authenticated provenance. See
[the Docker guide](docker_containerization.md) for storage and execution behavior.
Containers run with `--rm` cannot be inspected after removal. Do not rerun the
pipeline just to manufacture monitoring evidence. Existing milestone evidence
can instead be read with mutually exclusive `--docker-result`:

```bash
base=/home/negin/.local/share/aedp-docker-validation/20261123T112527292398Z
/home/negin/.venvs/aedp-pipeline/bin/python -m src.engineering_intelligence.monitor_pipeline \
  --data-dir "$base/live-data" \
  --docker-result "$base/live-results.json" \
  --docker-log "$base/live-compose.log" \
  --dbt-results "$base/live-data/dbt/target/run_results.json" \
  --parity-report "$base/live-data/reports/dbt_kpi_parity_20261123T113139473017Z.json"
```

## Freshness, completeness, and troubleshooting

Freshness is the age of an extraction at `checked_at_utc`, reported in **hours**
using the existing 24-hour stale threshold. It is separate from a PR's age at
extraction. A fresh latest run can coexist with stale retained PR observations
and mixed observation timestamps. No monitoring result verifies current GitHub
status or subsequent PR activity.

`pages_fetched` comes from stored metadata, never a URL containing `page=1`.
Three fetched pages can still be pagination-limited and incomplete. Exhausted
pagination is also not complete GitHub event history. Missing metadata remains
unknown. Partial source errors, rejected rows, and stored quality issues are
visible even when a different execution or validation succeeded.

| Exit code | Meaning | Next step |
| --- | --- | --- |
| `0` | Selected execution succeeded, required validation/data evidence is available, and no warnings/errors were found | Read provenance/limitations; this is not proof of complete history or production reliability |
| `1` | Observed execution/validation/source-quality failure, unreadable/malformed evidence, or report write failure | Read `observed_failures` and `evidence_errors`; inspect the existing task/container logs or failing dbt node and parity differences |
| `2` | Invalid command-line arguments | Correct the paths/options; Airflow needs both database and run ID |
| `3` | Warning, unknown/missing evidence, or in-progress execution, without an observed failure | Inspect warnings, finish/select the intended run, and supply its actual evidence; do not equate this with a failed pipeline |

Normal page caps produce exit `3`, not a fabricated pipeline failure. If an
upstream task failed, downstream `upstream_failed` remains visible. If DuckDB is
locked by a writer, inspection reports an evidence error; wait until execution
finishes and inspect again rather than bypassing the lock. Missing inputs are
never created as empty databases. Only the new monitoring output is written.

## Validation evidence: 2026-11-23

Deterministic tests use temporary DuckDB/SQLite files, fixed clocks, and controlled
Docker/dbt/parity artifacts. They cover success, failed and partial runs, stale
observations, unknown metadata, malformed artifacts, read-only connections,
argument binding, secret exclusion, and exclusive report creation. Existing CI
discovers these offline tests automatically; workflow behavior is unchanged.

Actual automated results:

- Full Windows suite: **231 discovered, 230 passed, 1 expected native Linux
  Airflow skip**, zero failures, 58.333 seconds. This includes real dbt builds and
  data tests against isolated fixtures.
- New monitoring tests in the existing Linux pipeline venv: **22/22 passed**,
  4.015 seconds. The same 22 tests passed on Windows.
- Existing native Linux Airflow focused suite: **18/18 passed**, 3.947 seconds.
  This validates DAG import/configuration, not a new DAG execution.

The monitor was also run against **existing historical evidence**, without
executing either pipeline again:

| Evidence selected | Actual inspection outcome |
| --- | --- |
| Airflow run `manual__2026-11-23T09:21:18.097597+00:00` | Execution success, 31.620794 seconds; ingestion 10.865520, warehouse 3.511787, dbt 14.001956, parity 0.650113 seconds; stored dbt/parity success |
| Existing Docker `live-results.json` and `live-compose.log` | Execution success, 15.149 seconds; all four completion markers; stage durations unknown because the saved logs lack timestamps; stored dbt/parity success |

Both inspections returned exit `3`. Airflow inspection found six repository
warnings: incomplete pagination, stale retained observations, and mixed
observation timestamps for each repository. Docker inspection found four
warnings: two incomplete-pagination warnings, unverified log association, and
unavailable stage timing. Both latest extractions recorded three pages and
incomplete pagination, with zero rejected records. Their freshness ages are
inspection-time values and will increase on later inspections.

New ignored reports preserved these inspections:

- `data/reports/monitoring/pipeline_monitor_20261123T145703636353Z_7bc67136.json`
- Docker validation directory's `live-data/reports/monitoring/pipeline_monitor_20261123T145704143725Z_75dd9003.json`

Hashes of seven existing operational inputs were unchanged after inspection.
Failure behavior is demonstrated by controlled tests, not a newly observed real
failed run. This milestone does not establish authenticated cross-artifact run
lineage, every past task attempt, live API availability, automatic alerts,
scheduled monitoring, or production-scale monitoring reliability.
