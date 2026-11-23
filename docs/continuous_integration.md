# GitHub Actions continuous integration

CI checks whether a proposed change still works before it joins `main`.
`.github/workflows/ci.yml` defines two independent jobs on standard GitHub-hosted
`ubuntu-24.04` runners with Python 3.12. The jobs run in parallel on pull requests
targeting `main` and pushes to `main`. They do not deploy or publish anything.
No scheduled workflow, service container, database service, model server, or
user-supplied credential is needed.

## Checks and dependency environments

| Check name | Dependencies | Tests |
| --- | --- | --- |
| **Python and dbt tests** | `orchestration/requirements-pipeline.txt`, including the existing `requirements-dbt.txt` | Entire existing `unittest` suite; real DuckDB/dbt integration tests on temporary fixtures |
| **Airflow DAG validation** | `orchestration/requirements-airflow.txt`, with Airflow 3.3.2 and official Python 3.12 constraints | Focused orchestration suite, including real native `DagBag` import, dependencies, trigger rules, and safety checks |

Each job creates its own `.venv` on a separate disposable runner. Airflow and dbt
never share a dependency environment. Existing version pins are reused without
changing the local dependency files or adding a testing framework. A prerequisite
step imports dbt and checks its adjacent executable before the full suite, so a
missing dbt installation cannot silently skip the integration class. The Airflow
job verifies Linux, Python 3.12, the pinned Airflow version, and its supported
`DagBag` import before testing. Native Airflow validation must execute there.

The full suite currently discovers 201 tests: 200 pass and the single native
Airflow test skips because that job deliberately has no Airflow installation.
The separate Airflow job runs all 18 orchestration tests, including that native
test, with no skips. These totals overlap; they are not 219 distinct tests.
Test counts will grow as the project adds coverage.

The dbt tests create controlled source/output DuckDB files in temporary
directories, build the real models, run data tests, compare known KPI results,
and check intentional invalid cases and repeatability. No existing warehouse,
raw snapshot, or report is required. A clean checkout with no ignored local data
is enough. dbt telemetry is disabled.

Airflow state is scoped to `$RUNNER_TEMP/airflow-ci` for its import/test steps,
with example DAGs disabled and the project DAG directory configured explicitly.
CI does not source the interactive `orchestration/environment.sh`, initialize the
production/development Airflow database, start `airflow standalone`, or trigger a
DAG. Importing a DAG checks its definition; it does not run its tasks.

## No live pipeline or inference

The workflow invokes existing tests, not the ingestion, warehouse, reporting,
Agent, or DAG entry-point commands. GitHub requests and Ollama responses are
mocked in those tests. Orchestration execution boundaries are mocked, while the
native import test reads the DAG definition only. There is no GitHub token/API key
for ingestion, model download, inference call, `airflow dags test`, or DAG trigger.

Dependency installation and action setup need Internet access to package/action
registries. The tests do not need live GitHub data or a running Ollama/Airflow
service. The workflow is not a network sandbox for arbitrary future PR code;
review changes that introduce external calls or new test commands.

## Security, reproducibility, and cost

- Workflow permission is explicitly `contents: read`; other token permissions
  are unset. Checkout uses `persist-credentials: false`. No custom secret is
  passed to dependency installation or tests.
- Only `pull_request` and `push` events are used. There is no privileged
  `pull_request_target` workflow or untrusted PR title/body interpolated into a
  shell command. Fork PRs run on disposable GitHub-hosted runners, subject to the
  repository's normal contributor-approval policy.
- Official `actions/checkout` and `actions/setup-python` references are pinned
  to verified full commit SHAs, with release-version comments. At implementation
  they correspond to checkout v7.0.1 and setup-python v7.1.0. Update the SHA and
  version comment together after reviewing the official release.
- `ubuntu-24.04` avoids an unexpected OS-major migration from `ubuntu-latest`.
  Python is fixed to the 3.12 series, allowing patch updates. Direct dependency
  pins and Airflow's release constraints improve repeatability; the pipeline's
  transitive dependencies and hosted OS image are not fully locked.
- Setup-python caches pip downloads using the relevant requirements-file hashes.
  It does not cache virtual environments, warehouses, raw data, credentials, or
  Airflow state. A cache miss is valid and simply reinstalls packages.
- Jobs have 20-minute limits. A newer commit cancels an older run for the same PR
  or branch, saving runner time. There is no broad matrix or duplicate feature
  branch push trigger in addition to a PR trigger.
- The repository was verified public. Standard GitHub-hosted runners are free
  for public repositories. No larger runners, paid action, artifact upload,
  deployment service, or billing setting is enabled. If the repository becomes
  private, review GitHub's included usage/storage and spending controls before
  assuming the same cost model. Do not enable paid cache overage for this setup.

See GitHub's [secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use),
[setup-python caching documentation](https://github.com/actions/setup-python#caching-packages-dependencies),
and [Actions billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Reading a failed check

Open the PR's **Checks** tab or the repository's **Actions** tab, select **CI**,
then open the failed job and its first failing step.

| Failure location | What to inspect |
| --- | --- |
| Checkout / Set up Python | Action policy, runner/package availability, or checkout permissions; not necessarily a Python regression |
| Install dependencies / `pip check` | Resolver output, supported wheels, constraints URL, or a dependency conflict; do not remove version constraints just to get green |
| Verify prerequisites | Missing/wrong Python, dbt executable, Airflow version, or import path; fix the environment before interpreting test results |
| Full Python suite | The `FAIL`/`ERROR` traceback and test name; dbt failures include captured command output from temporary fixtures |
| Airflow validation | Actual native import errors, task/dependency assertions, or a changed Airflow API; keep success-only trigger rules and the existing safety checks |

Some tests deliberately simulate timeouts, rejected records, and failed dbt data
tests. Such messages are expected when the enclosing unit test finishes `ok`.
The final unittest result and process exit code determine the check outcome.
An expected native Airflow skip in the pipeline job is covered by the independent
Airflow job; skips of dbt integration or native import in their designated jobs
need investigation. A canceled run was superseded; inspect the newest commit.
Waiting for contributor approval is a GitHub policy state, not a passing test.

The workflow does not create comments, modify code, auto-merge, or automatically
enforce branch protection. After the first hosted run, a maintainer can choose
**Python and dbt tests** and **Airflow DAG validation** as required checks in
repository rules. That configuration is outside this milestone.

## Reproducing the checks locally

Run the existing Windows suite with the existing `.venv`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For the Linux equivalent, use the existing isolated Linux environments from the
[Airflow guide](airflow_orchestration.md), or create fresh environments in a
separate temporary checkout. From that checkout, the workflow's relevant commands
are:

```bash
# Pipeline environment; do not overwrite the Windows .venv in a mounted checkout.
python3.12 -m venv /tmp/aedp-ci-pipeline
/tmp/aedp-ci-pipeline/bin/python -m pip install -r orchestration/requirements-pipeline.txt
/tmp/aedp-ci-pipeline/bin/python -m pip check
DBT_SEND_ANONYMOUS_USAGE_STATS=false /tmp/aedp-ci-pipeline/bin/python -m unittest discover -s tests -v

# Separate Airflow environment and temporary Airflow home.
python3.12 -m venv /tmp/aedp-ci-airflow
/tmp/aedp-ci-airflow/bin/python -m pip install -r orchestration/requirements-airflow.txt
/tmp/aedp-ci-airflow/bin/python -m pip check
export AIRFLOW_HOME="$(mktemp -d)"
export AIRFLOW__CORE__LOAD_EXAMPLES=false
export AIRFLOW__CORE__DAGS_FOLDER="$PWD/orchestration/dags"
export PYTHONPATH="$PWD"
/tmp/aedp-ci-airflow/bin/python -m unittest discover -s tests -p test_airflow_orchestration.py -v
```

Use unused temporary environment paths. These commands run tests only and must
not be replaced with a live end-to-end pipeline run in CI.

## Local validation and remaining hosted checks

Local validation on 2026-11-23 used fresh Linux checkouts exported from Git
(no ignored raw data/warehouses), new Python 3.12.3 virtual environments, and the
workflow's own shell steps. Both dependency installs passed `pip check`.

| Check | Local result |
| --- | --- |
| Fresh Linux pipeline job | 201 discovered: **200 passed, 1 expected native Airflow skip**; test duration 37.008 seconds |
| Final fresh Linux Airflow job | **18/18 passed**, including real DAG import; 1.503 seconds |
| Existing Windows environment | 201 discovered: **200 passed, 1 expected native Airflow skip**; 54.129 seconds |
| Workflow validation | YAML/security structure checks and actionlint v1.7.12 passed; all shell steps passed `bash -n` |

The linter initially found `runner.temp` used at job-level `env`, where that
context is unavailable. `AIRFLOW_HOME` was moved to the two Airflow import/test
steps. The corrected workflow was linted and the affected Airflow job retested.
Actionlint's optional shellcheck/pyflakes integrations were disabled; actual
shell execution and Python tests were validated separately. The lint executable
was downloaded to temporary Linux storage with its release checksum verified;
it is not a new project or CI dependency.

The ignored local report is
`data/reports/ci_local_validation_20261123T101923766778Z.json`. Fresh local jobs
used Python 3.12.3 on WSL2 Ubuntu 24.04; GitHub will supply its current 3.12 patch.
Installing into fresh virtual environments plus testing took about 67 seconds
for the pipeline job and 40 seconds for the final Airflow retest locally. Pip's
download cache may have been reused. These are local timings,
not guarantees for hosted runners or cache hits.

**GitHub-hosted execution has not happened.** It requires the user to push this
workflow and open/update a PR targeting `main`, or later push/merge to `main`.
No commit, push, PR creation, branch-rule change, or hosted run was performed.
Checkout action execution, setup-python downloads/cache integration, event
delivery, fork-approval policy, and visible check statuses must still be confirmed
on GitHub. Passing local job commands is not a claim that the hosted workflow has
already passed.

This milestone preserves all existing business logic, test files, dependency
files, and project data. CI covers tested code contracts and controlled dbt
transformations; it does not establish live API availability, model answer
accuracy, production reliability, or complete GitHub history. Deployment/CD,
dashboards, cloud infrastructure, and recurring pipeline scheduling remain planned.
