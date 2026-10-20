# Agentic Engineering Data Platform

A portfolio project for IT companies, built step by step while learning professional software and data engineering.

## Business problem

Engineering teams need reliable information about how work moves from pull request creation to closure and merge. Data spread across tools makes delivery bottlenecks difficult to identify. This project aims to turn engineering data into trustworthy metrics and eventually AI-assisted explanations that help teams investigate problems. Metrics should support team improvement rather than rank individuals.

## Completed functionality

- Multi-repository GitHub ingestion defaults to `dbt-labs/dbt-core` and `apache/airflow`, across open and closed PRs, sorted by creation time descending.
- Pagination follows GitHub's next-page links, with 30 PRs per page and a configurable default maximum of 3 pages per repository (up to 90 PRs each).
- New JSON snapshots in `data/raw/github/` preserve original PR objects plus repository, UTC extraction start/end times, source URL, record count, and per-page metadata.
- Pagination metadata records completeness, successful page count, stop reason, and the next or failed page URL. Successful pages survive later request failures; first-page failures produce metadata snapshots with zero records.
- Timeout, HTTP error, rate-limit, malformed response, and file-error handling, with offline mocked tests. Repositories are processed independently.
- Engineering warehouse in `src/engineering_intelligence/load_github.py` selects the latest metadata-valid snapshot per repository by embedded UTC extraction time and loads `data/processed/engineering.duckdb`.
- `ingestion_runs` stores snapshot provenance, pagination completeness, extraction errors, and quality results. `pull_requests` stores validated PR fields and the newest extracted observation per repository/PR number.
- Data Quality checks reject missing required fields, duplicate identifiers within a snapshot, invalid timestamps, and inconsistent lifecycle dates. Loading is transactional per snapshot and safe to rerun without duplicate PRs or runs.
- SQL analytics under `sql/analytics/` calculates repository-level observed PR counts, state breakdowns, merge rate, average/median time to merge, and open PR ages at each PR's extraction time, including counts older than 7 days.
- `report_kpis.py` reads the warehouse without modifying it, displays KPIs, and exports timestamped JSON under `data/reports/` with per-PR open ages, units/definitions, and ingestion completeness/error context. Controlled DuckDB tests check known results and edge cases.
- Four read-only agent data tools retrieve repository KPIs, bounded lists of observed stale open PRs with GitHub links, stored PR details, and data-quality/freshness status. Tool responses include observation timestamps, provenance, and limitations.
- Provider-neutral function schemas and an allowlisted dispatcher validate arguments and reject arbitrary SQL, unknown tool names, and unexpected fields. The data tools use local DuckDB and make no API calls.
- A fully local Ollama agent uses `qwen3:4b-instruct`, native function calling, bounded sequential tool rounds, and an interactive CLI. It uses existing `requests` and `AgentTools`, with no paid services, model SDK, API keys, or new packages.
- A local evaluation framework wraps the unchanged Agent with 18 version-controlled natural-language cases, deterministic offline tests, separate tool/argument/evidence/behavior scores, conservative KPI-number checks, trace/timing capture, and JSON reports. Every generated answer still requires human review.
- Archived weather proof of concept: fetch Turin hourly temperatures and load them into DuckDB without duplicate timestamps. The scripts in `archive/weather_poc/` still use the existing project-level data files.

GitHub ingestion is a snapshot, not complete history. Page-limited snapshots are explicitly incomplete, and the warehouse preserves that status. Even when pagination is exhausted, PRs can change during extraction and this endpoint does not provide every historical PR event. The warehouse stores current observed PR values, not every change over time. KPIs describe this observed, potentially incomplete cohort; they are not full-repository performance statistics or developer productivity rankings. The local Agent retrieves these observations and answers with evidence; generated prose still needs checking against that evidence.

## Run locally

From PowerShell in the project root, using the existing `.venv` with `requests` and `duckdb` installed:

```powershell
.\.venv\Scripts\python.exe .\src\engineering_intelligence\ingest_github.py
.\.venv\Scripts\python.exe .\src\engineering_intelligence\load_github.py
.\.venv\Scripts\python.exe .\src\engineering_intelligence\report_kpis.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Configure repositories and the page cap without editing Python:

```powershell
.\.venv\Scripts\python.exe .\src\engineering_intelligence\ingest_github.py --repositories dbt-labs/dbt-core apache/airflow --max-pages 3
```

`--max-pages` accepts a positive integer. A cap of 1 requests only the latest page. Public repositories need no credentials; none are embedded in the code.

The snapshot's `pagination.stop_reason` is `exhausted`, `page_limit`, `failed`, `partial_failure`, or `empty_page`. `pagination.complete` is true only when no next link remains. An empty page without a next link is valid; an empty page with a next link stops as incomplete. `pagination.next_url` records the remaining or failed page, not an automatic resume mechanism.

Rate limits stop the affected repository with retry guidance; remaining repositories are still attempted. The command exits with code 1 for API, response, or save failures (including an ambiguous empty page). Reaching the page cap is expected and exits successfully. File-write failures are reported, but cannot guarantee a saved snapshot.

Pagination divides results into smaller responses. The code uses `requests` to parse GitHub's `Link` header and follows its next URL, rather than constructing page numbers. See [GitHub pagination documentation](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).

API rate limits restrict request volume. Unauthenticated public requests normally allow 60 requests per hour per IP address, with additional secondary limits. The script reports `Retry-After` or `X-RateLimit-Reset` when available and does not retry automatically. See [GitHub rate-limit documentation](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

The warehouse loader uses existing snapshots and makes no API requests. It prints accepted records, actual rows inserted/updated, rejected records, and quality issues per repository. Rerunning unchanged snapshots normally reports zero rows inserted/updated. Invalid records remain in raw JSON; their reasons are also saved in `ingestion_runs.quality_issues`. Older single-page snapshots without pagination metadata have unknown completeness (`NULL`).

Malformed files or invalid extraction metadata are reported and skipped; valid files can still load, but the command exits with code 1. Selection is among metadata-valid snapshots, so an older valid snapshot may be selected when a newer file is invalid; this is reported as a failure, not silently treated as a clean load. Rejections, metadata quality issues, database failures, and source extraction errors also produce code 1. Incomplete page-limited data by itself is expected and does not fail the loader.

See [the warehouse guide](docs/warehouse.md) for the schema, upserts, quality policies, and SQL examples. See [the analytics guide](docs/analytics.md) for KPI formulas, SQL explanations, JSON structure, and interpretation limits. See [the agent tools guide](docs/agent_tools.md) for function calling, schemas, validation, and evidence handling.

Call a tool locally without a model or network access:

```powershell
.\.venv\Scripts\python.exe -c "import json; from src.engineering_intelligence.agent_tools import AgentTools; print(json.dumps(AgentTools().call_tool('list_stale_open_prs', {'repository': 'dbt-labs/dbt-core', 'min_age_days': 7, 'limit': 5}), indent=2))"
```

The tool list uses an inclusive age threshold (`>=`); the existing KPI for older than 7 days remains strict (`>`). Both use age at each PR's extraction, not age today. Source freshness is a separate checked-at-time measure with a documented 24-hour threshold. An empty result means no matching local observations, not verified absence on GitHub. Existing ingestion, warehouse, analytics, and archived code are preserved.

## Local Ollama Agent

Use the installed Ollama application and existing locally downloaded `qwen3:4b-instruct`. Verify that Ollama is running and `ollama list` includes that exact tag. If the server is not running, start Ollama or run `ollama serve` in another terminal. The app does not download a model or contact a cloud provider.

From the project root, start the interactive interface:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.local_agent --trace
```

Ask a self-contained question with a repository, such as: `Show two PRs observed open in dbt-labs/dbt-core with age at least 7 days, their links, observation times, and data-quality warnings.` Type `exit` or `quit` to finish. Each question starts a fresh conversation.

For one question:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.local_agent --question "What are the observed KPIs for apache/airflow? Explain the data limitations." --trace
```

The endpoint is fixed to `http://127.0.0.1:11434/api/chat`; proxies and redirects are disabled. The default is 4 tool-call rounds, with at most 4 calls per round. `--max-rounds` accepts 1 through 8. A bounded extra response request lets the model summarize after reaching its tool budget. Malformed/unknown tool requests produce error feedback; no text-to-code fallback exists. Python adds an evidence appendix independently of the model.

Natural repository comparisons do not need internal function names. The Ollama adapter explicitly describes available KPI fields, and the prompt asks for evidence from every named repository. Before accepting an answer, Python checks repository coverage and, for recognized metric questions, successful KPI coverage. A shared budget of up to two recovery reminders covers missing evidence and rejected answer drafts. Evidence recovery requests native calls; answer correction disables tools and uses existing evidence. Neither silently executes a query. The default maximum is seven model requests (`max_rounds + 1 + 2`), while the four-round tool cap stays unchanged.

`--trace` shows model requests, `no_tool_calls`, proposed calls, malformed calls, tool errors/successes, missing evidence, recovery/answer-recovery attempts, and final status. For example:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.local_agent --question "Between apache/airflow and dbt-labs/dbt-core, which has the higher median merge time? Explain the data limitations." --trace
```

See [the local Agent guide](docs/local_agent.md) for setup, loop architecture, security boundaries, limitations, and evaluation. Automated tests need no running Ollama. Real smoke tests used the installed Ollama 0.40.2/model and verified native tool selection, including a two-round list-then-inspect workflow. No commits or pushes are performed by the Agent.

Evaluate the Agent using the existing local model and warehouse:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.evaluate_agent --list
.\.venv\Scripts\python.exe -m src.engineering_intelligence.evaluate_agent --cases single-kpis compare-median stale-three specific-pr quality-one missing-repository missing-comparison unsupported-execution injected-fact
```

Omit `--cases` to run all 18 questions. Reports under `data/reports/agent_evaluation_*.json` retain every selected case, failure, tool result, recovery, and latency. Correct tool selection is measured separately from answer completion, and only recognized numerical claims are checked automatically. An operational pass is not a factual-accuracy score. See [the evaluation guide](docs/agent_evaluation.md) for scoring rules, actual local results, and the factual errors found during answer review. The existing Agent behavior and security controls are unchanged.

The 2026-10-20 local baseline evaluated 12 distinct cases: 10 operational passes (8 without repair, 2 recovery-assisted) and 2 routing/behavior-policy failures. Inspection also found incorrect pagination and freshness statements in passing answers. These results measure workflow behavior; they do not establish full answer accuracy. All 147 automated tests passed, including 25 new offline evaluation tests.

The subsequent [answer-grounding changes](docs/local_agent.md#targeted-answer-grounding) expose stored page counts and starting-URL semantics, render freshness hours/days in Python, and reject recognized unsupported metadata, staleness, causal, or productivity claims through the existing bounded rewrite mechanism. The original evaluation baseline is preserved; [follow-up results](docs/agent_evaluation.md#answer-grounding-follow-up) distinguish operational checks from prose review.

Before the focused reliability review, the suite passed 169 tests and the targeted local retest completed the median comparison but withheld the quality answer. The current suite passes 172 tests. After one presentation correction, the unchanged two-question retest passed 2/2 operational checks and the unchanged six-case set passed 6/6, with 2 and 6 recovery attempts respectively and no withheld answers. Prose inspection still found count/unit confusion, misleading timestamp wording, and metric-scope errors. These are operational results, not full factual accuracy; see the [focused review and limitations](docs/agent_evaluation.md#focused-reliability-review-2026-10-21).

The analytics script needs an existing warehouse with both tables. A valid empty warehouse produces an empty report; a repository with a run but no PRs has zero counts and unavailable (`null`) rates/durations. Generation time labels the output file only; it never affects open ages. `data/reports/` is excluded from Git, and reporting does not call GitHub or change raw snapshots, ingestion code, or warehouse tables.

Optional archived weather commands:

```powershell
.\.venv\Scripts\python.exe .\archive\weather_poc\fetch_weather.py
.\.venv\Scripts\python.exe .\archive\weather_poc\load_weather.py
```

Fetching weather again overwrites its raw JSON; loading reuses the database. GitHub ingestion creates a new snapshot each run. Raw and processed data are excluded from Git.

## Planned features (not implemented)

| Component | Intended purpose |
| --- | --- |
| Dashboard | Present engineering metrics and trends |
| Expanded agent workflows | Broader investigation features and expanded evaluation beyond the implemented local evaluation baseline |
| GCP | Host cloud storage and pipeline workloads |
| Airflow | Schedule and coordinate pipeline tasks |
| dbt | Build documented and tested SQL transformations |
| Docker | Package consistent runtime environments |
| Terraform | Define cloud infrastructure as code |
| CI/CD | Automate checks and later delivery of approved changes |

This is a learning roadmap, not an implemented production system. Exact cloud services and deployment choices remain undecided.

## Project layout

```text
archive/weather_poc/          Preserved, runnable weather scripts
src/engineering_intelligence/ Pipeline, KPI reporter, agent tools/schemas, and local Agent
evaluations/                Version-controlled Agent questions and expected behavior
sql/analytics/              Shared PR calculations and SQL report queries
tests/                      Pipeline, tools, Agent, and offline evaluation tests
docs/                       Architecture, pipeline, Agent, and evaluation guides
data/raw/                   Original API responses (ignored by Git)
data/processed/             Engineering and weather databases (ignored by Git)
data/reports/               Generated KPI and evaluation JSON reports (ignored by Git)
README.md                   Purpose, status, and run commands
.gitignore                  Local files excluded from Git
```

See [the architecture document](docs/architecture.md) for data flows and design decisions.
