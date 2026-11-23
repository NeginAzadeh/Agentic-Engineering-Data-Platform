# Engineering evidence and portfolio review

This summary was checked against the implementation, offline tests, saved local
JSON artifacts, read-only Airflow metadata, and GitHub-hosted CI metadata on
**2026-11-23**. It separates reproducible software checks from historical live
runs and model-answer accuracy. No live ingestion, Ollama inference, container
pipeline, or DAG execution was rerun for portfolio finalization.

## Reproducible checks

| Validation | Verified outcome | Evidence or reproduction |
| --- | --- | --- |
| Complete Windows suite | 231 discovered: **230 passed, 1 expected native Linux Airflow skip**, zero failures; 62.350 seconds | `python -m unittest discover -s tests -v` in the prepared pipeline environment; includes real isolated DuckDB/dbt builds |
| Native Linux Airflow checks | **18/18 passed**, zero skips; 3.592 seconds | Existing separate Airflow 3.3.2 venv, temporary Airflow home, `test_airflow_orchestration.py`; no task execution |
| Documented offline demo | Two accepted synthetic PRs, no quality issues; unchanged rerun writes zero; observed merge rate 50%, median merge time 2 hours, open age 9 days | [Exact walkthrough](demo.md); all data, dbt outputs, and reports written to a new OS temporary directory |
| Demo dbt build | **5 models + 52 data tests**, `PASS=57 WARN=0 ERROR=0 SKIP=0`; source attached read-only | Demo's generated `target/run_results.json`; 3 views and 2 tables |
| Demo KPI parity | **1 synthetic repository, 26 columns, zero discrepancies** | Existing `compare_kpis` implementation; both inputs read-only; `demo_parity.json` in the temporary directory |
| Demo tools and monitor | KPI tool `ok`; monitor execution `unknown`, stored dbt/parity `success`, warning code `3` | No execution source supplied; intentionally incomplete synthetic snapshot, with time-dependent freshness warnings |

The demo's first draft omitted the synthetic snapshot's `record_count`. The real
loader correctly reported a metadata quality issue and the walkthrough stopped.
The documented fixture now supplies the count; its full rerun passed. No loader
or validation rule was changed to make the example pass.

The test suites overlap: the native Airflow job reruns orchestration tests also
present in the full suite. Their totals are not independent tests to add together.
Historical milestone counts remain dated in their original guides.

## What the quality and transformation tests establish

- The loader rejects missing required values, within-snapshot duplicate PR
  identifiers, invalid/timezone-free timestamps, and inconsistent lifecycle
  dates. It records quality reasons and extraction errors without editing raw JSON.
  Snapshot content hashes and `(repository, pr_number)` keys make reruns safe.
- dbt tests cover non-null and unique identifiers, accepted states, relationships,
  composite repository/PR keys, lifecycle consistency, metadata consistency, and
  the source attachment's actual read-only flag. Custom SQL tests are checked in
  under [`dbt/tests/`](../dbt/tests/).
- Controlled tests check known counts, means/medians, `NULL` handling, extraction-based
  open ages, the strict seven-day boundary, mixed provenance, empty cohorts, and
  parity discrepancy detection. Parity compares the dbt mart with existing SQL;
  controlled expected values add a check beyond two implementations agreeing.
- Orchestration/container tests verify fixed commands, success-only ordering,
  lock contention, timeouts, and failure propagation. Monitoring tests cover failed
  and partial runs, stale/unknown metadata, malformed artifacts, read-only access,
  parameter binding, and exclusion of Docker environment values.

These tests do not prove source completeness, every possible API failure, global
cross-OS locking, production scale, or the semantic accuracy of every model answer.

## Historical operational evidence

Original JSON, databases, task logs, and authentication state are intentionally
ignored and remain local. A fresh public checkout contains the summaries and
tests, not those operational files. Reviewers can reproduce the isolated demo;
they should not expect ignored artifacts to be downloadable through relative links.

| Historical execution | Verified result | Supporting evidence |
| --- | --- | --- |
| Local dbt validation, 2026-10-31 | Five models and 52 tests passed; parity matched two repositories and 26 columns | [dbt validation guide](dbt_transformations.md#validation-results); saved parity/preservation JSON |
| Airflow on WSL2 Ubuntu 24.04, 2026-11-23 | DAG `engineering_data_pipeline`, run `manual__2026-11-23T09:21:18.097597+00:00`: all four tasks `success`; elapsed **31.620794 seconds** | Airflow SQLite queried read-only again during finalization; stored dbt results: 5 `success`, 52 `pass`; parity JSON with no differences |
| Docker Compose, 2026-11-23 | Existing isolated live-data run exited **0** in **15.149 seconds**; two repositories, 3 pages/90 PRs each, incomplete pagination; parity matched 26 columns | Saved `docker_validation_20261123T113643153997Z.json` and referenced result/log/parity artifacts; [Docker guide](docker_containerization.md#actual-validation-2026-11-23) |
| Docker offline negative control | Invalid fixture exited **1** during warehouse loading; downstream dbt/parity blocked; no transformed database | Same original Docker validation report; separate from its successful real run |
| Monitoring historical-run inspection | Airflow and Docker execution remained `success`, but both monitoring reports returned warning code **3** | [Monitoring validation](pipeline_observability.md#validation-evidence-2026-11-23): incomplete/stale retained Airflow observations; incomplete Docker snapshots and unavailable stage timing/verified log association |

Airflow task metadata records ingestion **10.865520 s**, warehouse **3.511787 s**,
dbt **14.001956 s**, and parity **0.650113 s**. The DAG interval includes gaps;
it is not the sum of task durations. Its previously supplied reported duration
was 31.620 seconds; the stored timestamps give the more precise interval above.

The current saved dbt invocation is dated `2026-11-23T09:21:48.620334Z`.
Airflow's saved parity report is
`data/reports/dbt_kpi_parity_20261123T092150851572Z.json`: `passed: true`,
2 repositories, 26 columns, empty differences, identical source hashes before
and after verification. Those hashes prove source preservation during the
verifier, not across ingestion/loading or all future builds.

Docker's original build used a temporary verified Buildx plugin. Permanent
Buildx 0.30.1 was installed later; this finalization does not claim the original
container tests were repeated using that installation. Legacy Docker logs lack
timestamped stage markers, so no per-stage Docker durations are invented.

## Hosted CI evidence

[CI run 45158463194](https://github.com/NeginAzadeh/Agentic-Engineering-Data-Platform/actions/runs/45158463194)
was checked through GitHub metadata: both **Python and dbt tests** and
**Airflow DAG validation** completed successfully. It belongs to the original CI
commit `d07d19cee6c4309390ba40f0298a42b8cb03b85b`.
[PR #11](https://github.com/NeginAzadeh/Agentic-Engineering-Data-Platform/pull/11)
is merged into `main`. This does not claim the current uncommitted portfolio
changes have run on GitHub, or that this earlier run contained today's test count.

The [workflow](../.github/workflows/ci.yml) uses separate Ubuntu 24.04/Python 3.12
jobs, pinned action SHAs, `contents: read`, and nonpersistent checkout credentials.
It runs mocked/isolated tests, not live ingestion, inference, or DAG tasks. See
[CI design and interpretation](continuous_integration.md) for dependency and
fork-approval limitations.

## Local Agent: operations are not factual accuracy

The four tool contracts and read-only SQL boundary are independently tested.
The model proposes tools/arguments; Python validates and executes only allowlisted
operations, bounds calls/recovery, and appends deterministic evidence. Guard
patterns can reject false claims or withhold an answer, but also have false
positives and can miss unsupported prose. They are not a semantic verifier.

The saved [evaluation history](agent_evaluation.md) was cross-checked against
original JSON summaries and answer text. These are small historical samples,
not a fresh benchmark against today's expanded warehouse:

| Recorded evaluation | Operational outcomes | Meaning and remaining problems |
| --- | --- | --- |
| Original 12 selected cases, across two reports | **10/12 passed**; two routing/behavior-policy failures | Even passing answers misstated page counts, freshness conversions, and productivity/causal implications |
| Earlier stricter two-case retest | **1/2 passed**, one withheld; 3 recovery attempts | Correct tools/evidence did not ensure an acceptable quality answer; this failure is preserved |
| Follow-up same two questions | **2/2 passed**, none withheld; one first-attempt pass, one recovery-assisted pass, 2 recoveries | Corrected quality answer completed, but comparison timestamp framing still needed review |
| Follow-up same six-case set | **6/6 passed**, none withheld; 3 first-attempt and 3 recovery-assisted passes; 6 recoveries, 8 tool calls | Four recognized KPI claims verified automatically; all answers still require review |

The last six-case report still contains the definite count/unit error **"90 PR
observations (measured in hours)"**, misleading shared-extraction-timestamp wording,
and generalizations from open-PR ages to all PRs. Unsupported review-bottleneck
interpretations and weak productivity-refusal rationale also remain documented.
A correct metadata appendix does not correct model prose retroactively.

Supporting ignored reports include
`agent_evaluation_20261020T195323429977Z.json`,
`agent_evaluation_20261020T195541940106Z.json`,
`agent_evaluation_20261020T211103817733Z.json`,
`agent_evaluation_20261020T220637213004Z.json`, and
`agent_evaluation_20261020T220919812941Z.json` under `data/reports/`.
The last two questions repeat in the six-case set; they are not eight independent
samples. Eighteen version-controlled cases exist, but no claim is made that the
final Agent passed all eighteen. No full-answer accuracy percentage is reported.

## Interpretation limits

Snapshots are bounded observations, not complete PR/event history. The source
warehouse preserves earlier PRs absent from later snapshots, so a cohort can
grow beyond 90 PRs and mix extraction times. `pages_fetched` comes from metadata,
not a starting URL's `page=1`. Fresh source metadata does not make every retained
observation fresh or prove a PR's current GitHub state.

Open ages use each observation's extraction timestamp; merge durations measure
creation to merge. Neither measures review effort or developer productivity.
Historical timing measurements are local observations, not performance targets.
Monitoring keeps execution, quality, and independent validation evidence separate;
it cannot authenticate Docker log/run association or validate today's transformed
database solely from an old report. Recurring scheduling, alerts, dashboards,
cloud deployment, and broader Agent investigations remain outside implemented scope.

## Final repository audit

Portfolio changes are documentation-only. The audit checked 14 Markdown files
and 76 relative links/anchors, including filename case, with no broken targets.
The demo's Python and all four PowerShell blocks passed syntax checks; the full
walkthrough was also executed successfully as described above. The Mermaid
flowchart's declared nodes, edge references, fences, and delimiters were checked
structurally. No graphical renderer was available, so its GitHub visual layout
remains a manual final-review check rather than a claimed rendering test.

Outdated warehouse/analytics/tool-guide statements about unimplemented Agent or
dbt functionality were corrected. Earlier validation results remain dated and
intact; the evaluation guide now points to its latest recorded review without
removing failures. The warehouse guide also distinguishes its original optional
timezone-package limitation from the currently installed environment.

All 210 checked pre-existing code, test, configuration, data, and generated-artifact
files retained their original SHA-256 hashes. Existing ignore rules cover raw and
processed data, reports, dbt artifacts/logs, virtual environments, and local
Airflow authentication files; no new ignore rule was necessary. No generated
databases, logs, or credentials were found among tracked paths. Four
high-confidence token/private-key patterns found no matches in the reviewed
worktree files; this is a scoped check, not a complete Git-history secret audit
or security certification. `git diff --check` passed. No source code, dependency,
CI behavior, pipeline behavior, database, snapshot, or historical report was changed.
