# Reproducible portfolio demo

## Choose a path

Start from the repository root in a local checkout. The default walkthrough
uses synthetic PR observations and a newly created OS temporary directory. It
calls the real loader, SQL analytics, dbt models/tests, parity checker, Agent
data tools, and monitoring code. It does not call the GitHub API or a model.
Original project data and historical reports are never used as write targets.

| Path | Needed beyond Python dependencies | External calls during execution |
| --- | --- | --- |
| Default walkthrough and offline tests | Python 3.12; Git only for obtaining the checkout | None |
| Container fixture validation | WSL2 Ubuntu 24.04, Docker Engine/Compose/Buildx, built local image | None with the documented `--network none` harness; image build needs package/registry downloads |
| Native DAG import tests | Separate Linux Airflow environment; WSL2 on Windows | None; no DAG tasks run |
| Optional interactive Agent | Local Ollama and the exact `qwen3:4b-instruct` tag already downloaded | Loopback Ollama chat only; downloading a model is a separate Internet operation |
| Intentional live pipeline | Docker or configured Airflow, plus network | Public GitHub REST API; creates new snapshots and updates the selected warehouse |

No payment, account, cloud service, API key, or Docker Hub login is required.
Do not run the last path as part of this offline review. Live setup and execution
remain separate, explicit procedures in the [Docker](docker_containerization.md)
and [Airflow](airflow_orchestration.md) guides.

## Prepare Python

Use Python **3.12**. On this project Windows and Linux have separate virtual
environments; never reuse the Windows `.venv` from WSL or install Airflow into it.
The direct pipeline requirements include `requests`, DuckDB, dbt Core, and
dbt-duckdb. dbt transitive dependencies are not completely locked.

If the project `.venv` already works, skip installation and use its Python. For a
fresh reviewer environment, create a new venv outside the checkout; these
PowerShell commands do not overwrite an existing environment:

```powershell
$demoVenv = Join-Path ([System.IO.Path]::GetTempPath()) ("aedp-review-" + [guid]::NewGuid().ToString("N"))
py -3.12 -m venv $demoVenv
$demoPython = Join-Path $demoVenv "Scripts\python.exe"
& $demoPython -m pip install -r .\orchestration\requirements-pipeline.txt
& $demoPython -m pip check
```

Installation requires Internet access. With the existing prepared environment,
set the same variable instead:

```powershell
$demoPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $demoPython --version
& $demoPython -m pip check
```

Expect Python 3.12 and `No broken requirements found.` Do not continue after a
failed installation or missing dbt executable. Run every command from the repo
root; `$demoPython` remains available only in the current PowerShell session.

## Walk through real transformations with synthetic data

Paste this complete block into that PowerShell session. The repository name is
a fixture label, not a repository being queried. Its source URL is metadata only.
The fixture intentionally marks pagination incomplete so the output demonstrates
why passing transformations and incomplete data can coexist.

```powershell
@'
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
from src.engineering_intelligence.agent_tools import AgentTools
from src.engineering_intelligence.load_github import load_warehouse
from src.engineering_intelligence.monitor_pipeline import main as monitor
from src.engineering_intelligence.report_kpis import build_report, display_report, write_report
from src.engineering_intelligence.verify_dbt_kpis import compare_kpis

project = Path.cwd()
demo = Path(tempfile.mkdtemp(prefix="aedp-demo-"))
data = demo / "data"
raw = data / "raw/github"
raw.mkdir(parents=True)
snapshot = {
    "repository": "demo/engineering", "extracted_at_utc": "2026-10-10T00:00:00Z", "record_count": 2,
    "source_url": "https://api.github.com/repos/demo/engineering/pulls?page=1",
    "pagination": {"complete": False, "pages_fetched": 1, "stop_reason": "page_limit"},
    "pull_requests": [
        {"number": 1, "title": "Synthetic open PR", "state": "open", "user": {"login": "fixture"},
         "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
         "closed_at": None, "merged_at": None},
        {"number": 2, "title": "Synthetic merged PR", "state": "closed", "user": {"login": "fixture"},
         "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T02:00:00Z",
         "closed_at": "2026-10-01T02:00:00Z", "merged_at": "2026-10-01T02:00:00Z"},
    ],
}
(raw / "fixture.json").write_text(json.dumps(snapshot), encoding="utf-8")
source = data / "processed/engineering.duckdb"
summaries, failed = load_warehouse(raw, source)
assert not failed and summaries[0]["loaded"] == 2
summaries, failed = load_warehouse(raw, source)
assert not failed and summaries[0]["loaded"] == 0  # unchanged rerun
with duckdb.connect(str(source), read_only=True) as connection:
    report = build_report(connection)
display_report(report)
write_report(report, data / "reports")

dbt = Path(sys.executable).with_name("dbt.exe" if os.name == "nt" else "dbt")
assert dbt.is_file(), "Install the pinned pipeline dependencies first"
env = dict(os.environ, ENGINEERING_PROJECT_ROOT=str(demo), DBT_SEND_ANONYMOUS_USAGE_STATS="false")
subprocess.run([str(dbt), "--log-path", str(demo / "logs"), "build",
                "--project-dir", str(project / "dbt"), "--profiles-dir", str(project / "dbt"),
                "--target-path", str(demo / "target")], env=env, check=True, timeout=180)
parity = compare_kpis(source, data / "processed/engineering_dbt.duckdb")
assert parity["passed"], parity["differences"]
parity_path = data / "reports/demo_parity.json"
parity_path.write_text(json.dumps(parity, indent=2), encoding="utf-8")
print("Parity:", parity["passed"], parity["repository_count"], parity["compared_column_count"])
print("Tool status:", AgentTools(source).get_repository_kpis("demo/engineering")["status"])
code = monitor(["--data-dir", str(data), "--dbt-results", str(demo / "target/run_results.json"),
                "--parity-report", str(parity_path)])
assert code == 3, "Expected warnings/unknown execution, not a pipeline success claim"
print("Demo directory:", demo)
'@ | & $demoPython -
if ($LASTEXITCODE -ne 0) { throw "Offline demo failed; inspect the first error above." }
```

Expected values for **this synthetic fixture**, verified during finalization:

| Output | Expected |
| --- | --- |
| First load | accepted 2, loaded/upserted 2, rejected 0, quality issues 0 |
| Unchanged second load | loaded/upserted 0; PR/run rows are not duplicated |
| Observed PRs | 2 total: 1 open, 1 merged, 0 closed without merge |
| Merge rate and duration | 50%; median and average creation-to-merge time 2 hours |
| Open age | 9 days at extraction; 1 open PR strictly older than 7 days |
| dbt build | 5 models and 52 data tests pass; 57 successful nodes |
| Parity line | `Parity: True 1 26` — one synthetic repository, 26 compared columns |
| Tool line | `Tool status: ok`; no LLM is involved |
| Monitoring | Execution `unknown`, stored dbt/parity `success`, incomplete-pagination warning, exit code 3; freshness warnings depend on inspection time |

The Python block exits zero only when these checks complete. It does not disguise
monitoring code `3` as pipeline success: no execution adapter was supplied.
Open age remains 9 days on later runs because extraction time is fixed. Freshness
changes with the current clock. Constructed `demo/engineering` links are fixture
references, not evidence that such a public repository or PR exists.

The printed demo directory contains raw JSON, both DuckDB files, generated dbt
artifacts/logs, the KPI/parity JSON, and a new monitoring report. You can inspect
those files; each repeat creates a different temporary directory. Keep or remove
only that directory through your normal OS tools when finished. Do not point
these commands at the repository's existing `data/`.

## Exercise negative and security cases offline

```powershell
& $demoPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
```

At finalization: 231 discovered, 230 passed, one expected native Airflow skip in
the pipeline environment. Missing dbt can cause integration tests to skip, so
the only expected skip here is the separate Linux Airflow import. Tests exercise
invalid timestamps, lifecycle inconsistencies, duplicates, missing metadata,
partial ingestion failures, blocked downstream execution, and mocked Agent
tool selection/recovery/injection scenarios. Some tests intentionally emit error
messages; the final unittest outcome determines whether the check passed.

## Optional deeper reviews

- **Docker:** follow only **Safe first validation** in the
  [Docker guide](docker_containerization.md#safe-first-validation). Use a new
  data directory and the existing network-disabled fixture harness, not the live
  Compose command. This runs all four real stages with mocked HTTP responses.
- **Airflow:** follow the separate environment setup and native import test in
  [the Airflow guide](airflow_orchestration.md). Import validation does not need
  a running UI or trigger a DAG. Starting or triggering Airflow is a separate,
  potentially data-writing action; it is unnecessary for this default demo.
- **Agent, without inference:** inspect [the schemas](../src/engineering_intelligence/tool_schemas.py)
  and [evaluation cases](../evaluations/agent_cases.json). Offline tests mock
  Ollama and exercise real tool dispatch. Listing cases with
  `& $demoPython -m src.engineering_intelligence.evaluate_agent --list` is also offline.
- **Agent, optional local inference:** needs the existing Ollama server/model and
  a populated local warehouse. See [setup and commands](local_agent.md#setup-and-commands).
  The Agent CLI defaults to the project's warehouse; it does not automatically
  select this temporary demo. Do not load synthetic data into the real warehouse
  to satisfy that default. No inference was rerun during portfolio finalization.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ModuleNotFoundError` | Start in the repo root and use the prepared venv, not a global Python or the Airflow venv |
| dbt missing / integration skips | Install `orchestration/requirements-pipeline.txt` in the chosen Python 3.12 environment; check `pip check` and the adjacent dbt executable |
| PowerShell `$demoPython` missing | Set it again in the current terminal; activation is unnecessary |
| dbt profile/source path error | Preserve the complete block's child environment and temporary paths; do not replace them with the live project root |
| DuckDB lock error | Close other connections to the same demo database; do not mix Linux and Windows writers |
| Monitoring exit 3 | Expected warnings or unknown execution; inspect the JSON rather than calling the pipeline failed or successful |
| dbt/quality/parity error | Inspect the first failure and retained temporary artifacts; do not bypass tests or force a passing exit |

See [engineering evidence](engineering_evidence.md) for what the historical live
runs establish and what neither this fixture nor CI can prove.
