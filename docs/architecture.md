# Project architecture

## Current GitHub pipeline

```text
GitHub REST API (dbt-labs/dbt-core and apache/airflow by default)
  -> src/engineering_intelligence/ingest_github.py
  -> data/raw/github/<timestamped snapshot>.json
  -> load_github.py (latest snapshot selection and Data Quality checks)
  -> data/processed/engineering.duckdb
       ingestion_runs + pull_requests
  -> sql/analytics/ (temporary calculations and read-only queries)
  -> report_kpis.py
  -> Console KPI summary + data/reports/<timestamped KPI report>.json

Mock API responses -> tests/test_github_ingestion.py
```

The module follows GitHub next-page links with up to 30 PRs per page across both states, sorted by creation time descending. The configurable default maximum is 3 pages per repository. Each repository produces a separate snapshot with original PR objects, extraction metadata, per-page URLs and counts, and pagination completeness. Previous single-page snapshots remain unchanged.

`check_response` handles HTTP and rate-limit errors; `fetch_pull_requests` collects bounded pages; `save_snapshot` writes new raw files; `ingest_repositories` isolates repositories; `main` parses command-line configuration and sets the exit code. These boundaries allow offline tests of network and storage failures.

Successful pages survive later API failures. Failure metadata is saved even when no page succeeds. Save failures are reported separately, and remaining repositories are still attempted. Rate limits stop the affected repository with retry guidance, without automatic retries. API or save failures produce a nonzero exit code; reaching the page cap is an expected bounded extraction.

Pagination is complete only when no next link remains. A cap, failure, or empty page with a next link is incomplete. Snapshots are not complete event history: PRs can change between page requests. The warehouse preserves completeness metadata and never deletes PRs simply because they are absent from a later bounded snapshot. There is no scheduler yet.

## Implemented warehouse

`load_github.py` reads existing raw files without calling GitHub or modifying JSON. It selects the newest metadata-valid extraction per repository, validates records, and writes two DuckDB tables. Discovery, validation, schema creation, and loading are separate functions so tests can use temporary files and real isolated databases.

`ingestion_runs` records one row per selected snapshot, identified by its SHA-256 content hash. `pull_requests` uses the composite primary key `(repository, pr_number)` and upserts only observations from a newer extraction. All warehouse timestamps are timezone-aware and normalized to UTC. Each snapshot's run metadata and PR changes commit together, or roll back together if database writes fail.

Rejected records remain available in raw JSON, and their record indices, PR numbers, and reasons are recorded in run metadata. Missing pagination metadata means unknown completeness, not complete data. Invalid files are reported, while remaining valid repositories can load. The loader does not backfill all archived snapshots or maintain PR event history. See [the warehouse guide](warehouse.md) for schema and quality rules.

## Archived weather proof of concept

```text
Open-Meteo -> archive/weather_poc/fetch_weather.py
  -> data/raw/turin_temperature_2026-09-01_to_2026-09-03.json
  -> archive/weather_poc/load_weather.py
  -> data/processed/weather.duckdb (weather_hourly table)
```

Both scripts locate the project root with `Path(__file__).resolve().parents[2]`, preserving access to existing data regardless of the terminal's working directory. The loader checks list lengths and prevents duplicate timestamps using a primary key. This learning example is separate from the active GitHub pipeline.

## Implemented analytics

`sql/analytics/pr_observations.sql` defines a session-only view with one row per stored PR, elapsed merge time, and open age at that PR's own extraction timestamp. `repository_kpis.sql` aggregates by repository before joining the latest ingestion metadata, preventing count inflation from multiple runs. `open_pr_ages.sql` lists every observed open PR's age and its observation time.

`report_kpis.py` uses a read-only connection and a transaction so both queries see the same warehouse state. The temporary view is not persisted. Python formats console output and exports structured JSON; SQL owns the KPI formulas. Report generation time is separate from metric inputs. The warehouse, raw files, and existing ingestion functions remain unchanged.

Both latest ingestion context and contributing observation-run context are retained. Older PRs can remain after later bounded or failed extractions, so this is an observed cohort with potentially mixed extraction times, not a complete or simultaneous view of repository activity. See [the analytics guide](analytics.md) for definitions and SQL learning examples.

## Planned downstream pipeline (not implemented)

The read-only local Ollama Agent is implemented separately from its data tools; see below. Broader investigation workflows and dashboards remain planned.

```text
Existing warehouse and SQL KPI report
  -> dbt-managed transformations
  -> Dashboard and expanded Agent workflows
```

The dashboard would present defined metrics. Expanded agent workflows would require more evaluation and evidence sources. No dashboard is implemented; the current local assistant only calls the four existing tools.

## Implemented agent tool foundation

```text
Trusted local caller (or local Ollama model proposes name and JSON arguments)
  -> tool_schemas.py + AgentTools.call_tool allowlist and runtime validation
  -> Four fixed operations in agent_tools.py
  -> Read-only DuckDB connection + existing checked-in analytics SQL
  -> Structured data + provenance + freshness/quality + explicit limitations
```

The four operations are `get_repository_kpis`, `list_stale_open_prs`, `get_pr_details`, and `get_data_quality_status`. Inputs cannot choose SQL, SQL file paths, database paths, or additional operations. All input values passed to SQL are separate bound parameters. Database path and test clock are configured by trusted application code.

Each call opens a read-only connection and uses one transaction for context and detail queries. Only session-local views reuse the existing KPI formulas; no warehouse changes are made. Stale PR listing has a validated result cap and supplies constructed GitHub PR references. Source text is untrusted data, never execution instructions.

PR age stays reproducible at extraction. Source freshness is separately checked against an explicit current UTC timestamp and a 24-hour threshold. Latest ingestion metadata and contributing PR-run metadata are both exposed, so a recent failed attempt or clean later run cannot hide old observations or previous quality issues. Unknown metadata stays unknown. These tools never verify live GitHub status or infer review activity, root causes, or developer performance.

Schemas remain provider-neutral. The local Agent wraps them in Ollama's native function format. The tools themselves make no network requests. See [the agent tools guide](agent_tools.md).

## Implemented local Agent

```text
Interactive CLI question
  -> local_agent.py: system policy + four Ollama function schemas
  -> POST http://127.0.0.1:11434/api/chat (qwen3:4b-instruct)
  -> Native assistant tool_calls
  -> Validate shape -> AgentTools.call_tool -> read-only DuckDB
  -> JSON tool messages marked as untrusted data
  -> Next local model turn (bounded loop)
  -> Model answer checks + application-rendered evidence/limits
```

The model chooses operations, arguments, and phrasing. Python controls access, schemas, budgets, failures, and tool execution. The default cap stays at 4 tool-call rounds and 4 calls per round. Up to 2 evidence-recovery reminders can add model requests, for a hard default total of 7 (`max_rounds + 1 + max_recoveries`). Malformed attempts consume the tool-round budget. Multiple requests within a batch execute sequentially.

The Ollama adapter describes the existing KPI fields, including median/average merge hours, without modifying the original schemas or tools. Repository identifiers are extracted and validated from the user question, never from source data. Before accepting a draft, the application requires successful evidence for each named repository; recognized analytics intents additionally require the KPI operation for each. Missing evidence triggers a bounded reminder, and only the model's subsequent native calls can execute. Failed or not-found calls do not satisfy coverage. Repository extraction and analytics intent detection are conservative heuristics, not complete natural-language understanding.

Structured trace events distinguish requests, native call counts, no-call responses, malformed calls, error codes, missing evidence, recovery, and final status. Diagnostics include available Ollama stop reason and token counts. Premature prose is discarded rather than replayed as authority; recovery messages use only validated user-scope identifiers and fixed instructions.

The same two-attempt recovery budget can correct a rejected final draft. That phase removes available tools, permits no new tool execution, and asks for a rewrite from existing evidence. A narrow check rejects claims that distinct repository extraction ranges share one timestamp. This supplements, rather than replaces, the evidence appendix and the limitations of free-form prose checks.

Only native structured calls execute; apparent code in generated text or PR titles is never interpreted. Tool outputs cannot become system messages. All retrieved text is untrusted. Proxy routing and redirects are disabled; the model/endpoint are fixed to the tested local configuration. No model SDK, paid API, API key, or framework is introduced.

Answers without successful tool evidence are withheld. Unreturned URLs, certain explicit current-status claims, and executable code blocks are also withheld. Python always adds returned PR references, observation times, and available quality/freshness warnings. These checks are not a semantic proof of every sentence; generated prose must still be reviewed against source evidence. See [the local Agent guide](local_agent.md) for evaluation and operational limits.

Airflow would coordinate tasks; Docker would package their runtime. GCP would host cloud resources, Terraform would define infrastructure, and CI/CD would automate checks and later delivery. Specific services, metric definitions, and deployment design remain undecided.

## Folder responsibilities

The implemented evaluation layer sits outside the Agent: version-controlled `evaluations/agent_cases.json` -> `evaluate_agent.py` -> unchanged `LocalAgent.ask` -> operational/numerical checks -> ignored JSON report and human review. Expected operations and independent warehouse reference reads never enter model messages. See [the evaluation methodology and actual results](agent_evaluation.md).

- `src/engineering_intelligence/`: pipeline, reports, agent tools/schemas, and local Ollama loop/CLI.
- `sql/analytics/`: PR calculations and repository KPI queries.
- `tests/`: mocked API tests and isolated DuckDB warehouse/analytics tests.
- `evaluations/`: natural-language questions, expected tools/arguments, and review criteria.
- `archive/weather_poc/`: preserved and runnable learning example.
- `data/raw/`: original API responses.
- `data/processed/`: local engineering and weather databases.
- `data/reports/`: generated KPI and Agent evaluation JSON, excluded from version control.
- `docs/`: design documentation.

Data and `.venv/` remain local through `.gitignore`. Ingestion, quality checks, warehouse, KPIs, agent tools, and local Ollama integration are implemented. dbt, dashboards, scheduling, cloud infrastructure, and broader Agent workflows remain planned.
