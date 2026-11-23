# Docker Compose pipeline

## Implemented scope

One one-shot Compose service runs the existing four stages in order:

```text
GitHub ingestion -> DuckDB warehouse loading -> dbt build -> KPI parity
```

The image contains the three existing Python entry modules, existing analytical
SQL, dbt project, pipeline dependency pins, and the standard-library execution
helpers from `orchestration/pipeline_runtime.py`. The small
`containerization/run_pipeline.py` entry point supplies the sequence; it does not
implement ingestion, validation, SQL, or KPI formulas again. One container avoids
unnecessary inter-service coordination for these sequential batch commands.

Airflow, its DAG/scheduler, Ollama, and the Agent are outside this image. There
are no PostgreSQL, Redis, Kafka, Kubernetes, cloud services, paid APIs, exposed
ports, or Docker socket mounts. No Docker Hub account, login, or image publication
is required. Existing Airflow and GitHub Actions files are unchanged.

## Runtime and prerequisites

Use Docker Engine, Docker Compose v2, and Buildx on **WSL2 Ubuntu 24.04**. Docker
Desktop is not required. On this machine, Docker Engine 29.1.3 and Compose
2.40.3 were installed, but:

- The normal WSL user could not access `/var/run/docker.sock`; passwordless sudo
  was unavailable. Validation used WSL root for Docker commands only. Containers
  still ran as UID/GID 1000. No group membership or socket permission was changed.
- Buildx was missing. Validation used Docker's official Buildx v0.39.1 binary in
  a temporary, isolated `DOCKER_CONFIG`, verified against its release checksum.
  No system package or existing Docker credential/configuration was changed.

For normal user-operated builds, install the missing Ubuntu package if needed:

```bash
# User action only; this system installation was not performed during the milestone.
sudo apt install docker-buildx
docker compose version
sudo docker version
sudo docker buildx version
```

`apt-cache policy` confirmed `docker-buildx` is available for this Ubuntu
installation. Use `sudo docker ...` when required rather than weakening socket
permissions. Docker daemon access is privileged even when containers run as
non-root. If the image is already built locally, running it does not need Buildx;
future image builds do.

The Dockerfile uses the official Python 3.12 slim Bookworm image pinned by a
manifest digest, not a floating base alone. The built image reports Python
3.12.16. Existing pins for dbt Core 1.12.6, dbt-duckdb 1.11.1, DuckDB 1.5.6, and
requests 2.34.2 are reused through `requirements-pipeline.txt`. No Airflow extras
or model dependencies are installed. Transitive Python dependencies are not
fully locked, so future rebuilds can resolve different compatible versions;
retain the tested image or add a reviewed lock in a later milestone.

## Storage and path contract

Compose requires `PIPELINE_DATA_DIR`; there is deliberately no default pointing
at the repository's existing data. It must name an existing **absolute Linux
directory**, not `C:\...`. `create_host_path: false` prevents automatic creation
of a mistyped mount source. The entry point also refuses to run without an
explicit `/app/data` mount or as root.

| Host | Container | Purpose |
| --- | --- | --- |
| Chosen `PIPELINE_DATA_DIR` | `/app/data` | Persistent raw snapshots, databases, reports, dbt artifacts |
| `<data>/raw/github/` | `/app/data/raw/github/` | New timestamped snapshots |
| `<data>/processed/engineering.duckdb` | `/app/data/processed/engineering.duckdb` | Warehouse loaded by Python; attached read-only by dbt |
| `<data>/processed/engineering_dbt.duckdb` | `/app/data/processed/engineering_dbt.duckdb` | Separate transformed tables |
| `<data>/reports/` | `/app/data/reports/` | KPI parity reports |
| `<data>/dbt/target/`, `<data>/dbt/logs/` | Corresponding paths under `/app/data/dbt/` | Generated dbt artifacts/logs |
| Image source files | `/app/src`, `/app/sql`, `/app/dbt`, helpers | Read-only application code/configuration |

dbt's target/log environment variables redirect artifacts to the data mount;
the existing profile is unchanged. `/tmp` is a disposable 256 MiB tmpfs. The
container root filesystem is read-only, capabilities are dropped, privilege
escalation is disabled, and an init process forwards signals/reaps processes.
Default UID/GID is `1000:1000`; `PIPELINE_UID`/`PIPELINE_GID` can match another
non-root Linux user. The selected data directory must already be writable by
that user. Do not recursively change ownership of existing project data.

The `.dockerignore` filters source/configuration paths and explicitly excludes
`.git`, virtual environments, data, logs, dbt targets/packages, credentials,
authentication files, keys, and caches. Dockerfile `COPY` statements select the
runtime modules rather than copying the whole repository. Tests and fixture
harnesses are mounted read-only for validation, not shipped in the image.

## Safe first validation

Run these commands in the normal **WSL Ubuntu terminal** from the repository root.
Use a new directory; do not mount the existing repository `data/` for validation:

```bash
cd -- "$(wslpath -u 'C:\Users\Negin\Documents\ai-data-quality-platform')"
VALIDATION_ROOT="$(mktemp -d "$HOME/aedp-docker-test.XXXXXX")"
export PIPELINE_DATA_DIR="$VALIDATION_ROOT/data"
mkdir "$PIPELINE_DATA_DIR"
export PIPELINE_UID="$(id -u)"
export PIPELINE_GID="$(id -g)"

sudo --preserve-env=PIPELINE_DATA_DIR,PIPELINE_UID,PIPELINE_GID docker compose config --quiet
sudo --preserve-env=PIPELINE_DATA_DIR,PIPELINE_UID,PIPELINE_GID docker compose build
```

`config` checks interpolation/configuration, not a successful pipeline run.
`build` installs packages and creates the local image without mounting or loading
host databases. Build/pull operations need Internet access; anonymous public
registry rate limits may require waiting, not a subscription or login.

Before live ingestion, execute the fixture harness with networking disabled:

```bash
sudo docker run --rm --init --network none --read-only \
  --user "$PIPELINE_UID:$PIPELINE_GID" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,nosuid,nodev,size=268435456,mode=1777 \
  --mount "type=bind,src=$PIPELINE_DATA_DIR,dst=/app/data" \
  --mount "type=bind,src=$PWD/tests/docker_fixture_pipeline.py,dst=/validation/fixture.py,readonly" \
  --entrypoint /opt/venv/bin/python \
  agentic-engineering-pipeline:local /validation/fixture.py
```

This mocks only the GitHub HTTP response inside the ingestion subprocess. All
four existing stages actually execute, with synthetic PRs and real DuckDB/dbt.
Successful fixture parity is not evidence of a live GitHub extraction. For the
negative control, use a **different new empty** data directory and append
`--invalid` to the harness command. It must exit 1 during loading, reject the bad
timestamp, and create no transformed database. A partial source warehouse may
exist because the original loader commits valid records before reporting issues.

Run unit/integration tests inside the image without enabling network access by
mounting only the test directory read-only and overriding the entry point:

```bash
sudo docker run --rm --init --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=268435456,mode=1777 \
  --mount "type=bind,src=$PIPELINE_DATA_DIR,dst=/app/data" \
  --mount "type=bind,src=$PWD/tests,dst=/app/tests,readonly" \
  --entrypoint /opt/venv/bin/python agentic-engineering-pipeline:local \
  -m unittest discover -s tests -p test_containerization.py -v
```

Repeat with `-p test_dbt_transformations.py` for real isolated dbt integration
tests. The runtime image intentionally excludes Agent/evaluation modules; run
the complete project suite in the existing host/CI environment rather than
assuming the image is a full development environment.

## Intentional live execution and persistence

After offline validation, choose another new directory for live data. The sample
below keeps it separate from both fixtures and the original host data:

```bash
export PIPELINE_DATA_DIR="$HOME/.local/share/aedp-docker-data"
mkdir -p "$PIPELINE_DATA_DIR"
export PIPELINE_UID="$(id -u)"
export PIPELINE_GID="$(id -g)"

sudo --preserve-env=PIPELINE_DATA_DIR,PIPELINE_UID,PIPELINE_GID \
  docker compose -p aedp-pipeline run --rm pipeline
echo "Pipeline exit code: $?"
```

This is the real pipeline: it calls the public GitHub API and intentionally
updates only the chosen directory. The host files survive `--rm`, container
replacement, or `docker compose down`. Original repository data is never mounted
by default. Reusing a live directory preserves raw snapshots and applies existing
warehouse upsert behavior; it is not a transaction spanning all stages.

Airflow/Ollama are not started or contacted. The CLI accepts no arbitrary commands
or model-proposed arguments. Docker's explicit `--entrypoint` override is used
only by trusted manual tests above, not by the production entry point.

## Bounds, exit codes, and concurrency

The entry point reuses `STAGES`, `STAGE_TIMEOUTS`, `command_for`, and
`execute_command` from the existing runner. Time budgets remain 300 seconds for
ingestion, 180 for loading, 600 for dbt, and 120 for parity. Each child uses fixed
argument lists with no shell interpolation. The first failure prevents all later
stages; there are no automatic container restarts/retries.

Exit 0 means every stage succeeded. Ordinary child failures preserve their exit
code (for example 1 for quality/parity failures); configuration failures return 1,
unsupported arguments 2, process timeouts 124, Ctrl+C 130, and SIGTERM 143.
Signals trigger the existing process-group cleanup; Compose gives 20 seconds
before forced termination. A forced kill can leave partial valid output, so
inspect logs/data rather than blindly retrying or deleting files.

The shared `processed/.airflow_pipeline.lock` is held across the entire Docker
run. Child processes inherit its descriptor. Competing compliant runners cannot
write simultaneously. The file's existence does not mean a lock is held; never
delete it to bypass contention. This is cooperative Linux locking, not a global
cross-OS lock or a replacement for pipeline ownership. Do not run Airflow,
standalone Python loaders, or another pipeline against the same data directory
concurrently; Airflow's unchanged locks are per task, so separate runs could
otherwise interleave between stages. Windows writers do not participate.

dbt still attaches the source read-only and writes the separate transformed
database. Open PR age remains measured at extraction, merge duration remains
creation to merge, and merge rate uses the observed cohort. Three fetched pages
are not complete repository history. A successful container run does not imply
freshness indefinitely, current GitHub state, or causal/productivity conclusions.

## Actual validation: 2026-11-23

Validation first used isolated synthetic data with networking disabled, then a
different new directory for live public API data. Existing project snapshots and
databases were not mounted in either run.

| Check | Actual result |
| --- | --- |
| Compose configuration | Passed; missing data-directory variable fails closed |
| Dockerfile/build checks | Passed with no warnings; local image built without login or publication |
| Actual exported build context | No forbidden data/cache/generated/authentication paths found |
| Image audit | Python 3.12.16, UID 1000; no Airflow/Ollama installation, Agent module, `.git`, Windows venv, or generated dbt target/log directories |
| Container unit tests | 8/8 passed, network disabled |
| Container real dbt integration tests | 11/11 passed, network disabled |
| Valid fixture pipeline | All four stages passed; 5 models + 52 dbt tests; parity matched 2 repositories and 26 columns |
| Invalid fixture pipeline | Exit 1 during warehouse quality checks; no dbt output database; downstream stages blocked |
| Missing mount / kernel lock controls | Unmounted entry point exited 1; real conflicting Linux lock rejected |
| Real live Compose pipeline | Exit 0 in **15.149 seconds**; all four stages succeeded |
| Live extraction | `apache/airflow` and `dbt-labs/dbt-core`: 90 PRs and 3 pages each, both pagination-incomplete |
| Live dbt/parity | 5 models + 52 tests passed; **2 repositories, 26 columns, 0 discrepancies** |
| Full Windows test suite | 209 discovered: **208 passed, 1 expected native Linux Airflow skip**, zero failures, 34.284 seconds |
| Native Linux Airflow focused suite | 18/18 passed; Airflow implementation unchanged |

Validation output is retained in the newly created Linux directory
`/home/negin/.local/share/aedp-docker-validation/20261123T112527292398Z/`:
`fixture-data/`, `invalid-data/`, `test-data/`, `live-data/`, context audit,
per-check logs, and `offline-results.json`/`live-results.json`. Live parity is
`live-data/reports/dbt_kpi_parity_20261123T113139473017Z.json`, also visible inside
the live container as `/app/data/reports/dbt_kpi_parity_20261123T113139473017Z.json`.
The verifier's before/after source hashes matched. The tested local image ID is
`sha256:f62e77c9cfd00b97c557141edb135dcb5de76048c5663c2daaf2bc2e91e36ed8`;
reported local image size is 167,008,394 bytes (about 159 MiB), not peak RAM use.

The consolidated ignored report is
`data/reports/docker_validation_20261123T113643153997Z.json`. SHA-256 comparisons
confirmed that all 79 checked pre-existing data, source, test, dependency-related
runtime, SQL, dbt, archived, and CI files stayed unchanged during validation.
Documentation was updated separately. No existing snapshot, database, or report
was overwritten or deleted.

## Cleanup and limits

`run --rm` removes the completed container. To remove this project's unused
Compose network after stopping active work, use the same data environment and
`docker compose -p aedp-pipeline down`. It does not delete bind-mounted data.
Validation used a separate project name, `aedp-validation-20261123`. Do not use
global prune commands or remove host data as routine cleanup. Logs, validation
directories, and the local image are retained for review; no automatic deletion
is implemented.

Only Linux/amd64 was tested. The image is a local batch runtime, not a scheduler,
deployment platform, complete dependency lock, image-vulnerability attestation,
or historical GitHub backfill. Registry/package access is required to build, and
live execution needs public GitHub connectivity/rate allowance. Docker testing
was not added to GitHub Actions; its existing two-job behavior is preserved, and
the new offline unit tests are discovered by the existing full-suite command.
No commit, push, merge, PR, or image publication was performed.

References: [Docker build practices](https://docs.docker.com/build/building/best-practices/),
[build-context exclusions](https://docs.docker.com/build/concepts/context/),
[Compose services and mounts](https://docs.docker.com/reference/compose-file/services/),
[official Python image](https://hub.docker.com/_/python).
