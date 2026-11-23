# Engineering warehouse — Part 1

## Run

From PowerShell in the project root, with the existing `.venv`:

```powershell
.\.venv\Scripts\python.exe .\src\engineering_intelligence\load_github.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

DuckDB is already installed. The loader reads local JSON, never calls GitHub, and writes `data/processed/engineering.duckdb`. `.gitignore` excludes this database and raw JSON. The archived weather database is separate.

## Snapshot selection

The loader scans JSON files directly inside `data/raw/github/`. It groups files by the embedded repository name and selects the greatest `extracted_at_utc`, normalized to UTC. It does not trust file modification times or filename sorting. Repository names are normalized to lowercase for database keys. Equal extraction times use the first filename in sorted order, deterministically.

Malformed JSON and invalid extraction metadata are reported and skipped, with a nonzero command exit code. Selection then uses metadata-valid files; if a newer file is invalid, an older valid file can load, but the command still reports failure. It does not silently claim the freshest data loaded. Metadata must include repository, a timezone-aware extraction time, source URL, and a PR list. Optional metadata must have valid types.

Only the latest selected snapshot per repository is loaded, not every archived snapshot. A latest failed extraction is still recorded; it does not delete previously loaded PRs.

## Tables and types

A **primary key** identifies one row and prevents duplicates. `NOT NULL` requires a value. A nullable field can contain SQL `NULL`, meaning no value is available; it is different from an empty string.

### ingestion_runs

| Column | Type | Meaning |
| --- | --- | --- |
| `run_id` | `VARCHAR PRIMARY KEY` | SHA-256 hash of the original snapshot bytes |
| `repository` | `VARCHAR NOT NULL` | Normalized `owner/name` |
| `extracted_at` | `TIMESTAMPTZ NOT NULL` | Extraction start time |
| `finished_at` | `TIMESTAMPTZ`, nullable | Extraction end time when available |
| `source_url`, `snapshot_file` | `VARCHAR NOT NULL` | API URL and raw file location |
| `pagination_complete` | `BOOLEAN`, nullable | True, false, or unknown for legacy snapshots |
| `pages_fetched` | `INTEGER`, nullable | Successfully fetched pages |
| `stop_reason` | `VARCHAR`, nullable | Original pagination stop reason |
| `source_error` | `VARCHAR`, nullable | Original ingestion error |
| `record_count` | `INTEGER NOT NULL` | Actual number of raw PR records |
| `accepted_records`, `rejected_records` | `INTEGER NOT NULL` | Quality-check outcomes |
| `quality_issues` | `VARCHAR NOT NULL` | JSON text with record indices, PR numbers, and reasons |

Hashing the contents gives an unchanged file the same ID even if copied or renamed. Rerunning it cannot add another run. Editing the bytes creates a new snapshot identity. This table records source extractions processed by the loader, not every invocation of the loader.

### pull_requests

| Column | Type | Meaning |
| --- | --- | --- |
| `repository` | `VARCHAR NOT NULL` | Repository identity |
| `pr_number` | `BIGINT NOT NULL` | Positive integer PR number |
| `title`, `author` | `VARCHAR NOT NULL` | PR title and `user.login` |
| `state` | `VARCHAR NOT NULL` | `open` or `closed`, enforced by a SQL check |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL` | Creation and last-update instants |
| `closed_at`, `merged_at` | `TIMESTAMPTZ`, nullable | Closing and merging instants |
| `source_extracted_at` | `TIMESTAMPTZ NOT NULL` | Timestamp of the observation used for this row |
| `last_run_id` | `VARCHAR NOT NULL` | Provenance ID of the supplying run |

The composite primary key is `(repository, pr_number)`: PR #1 in one repository is different from PR #1 in another. `last_run_id` is a logical link to `ingestion_runs`; this simple schema does not add a foreign-key constraint. Run and PR writes happen in the same transaction.

`VARCHAR` stores text, `INTEGER` and `BIGINT` store whole numbers, and `BOOLEAN` stores true/false. `TIMESTAMPTZ` represents an absolute instant. Python parsing rejects timezone-free input and converts offsets to UTC; the loader sets DuckDB's session timezone to UTC for display. See [DuckDB timestamp types](https://duckdb.org/docs/current/sql/data_types/timestamp).

## Data Quality policy

- Require a positive integer PR number, nonblank title and author, valid state, and creation/update timestamps. A missing/deleted author is rejected under this initial policy.
- Reject every copy of a duplicated PR number within a snapshot, rather than choose an arbitrary winner. Repository grouping makes this a repository/PR identifier check.
- Require valid ISO timestamps containing a timezone, including any non-null closing/merging dates.
- Require creation no later than update, closure, or merge; closure and merge no later than update; and merge no later than closure.
- Open PRs must have no closing/merging dates. Closed PRs require a closing date. A closed PR without a merge date is valid.
- Report mismatched metadata record counts while still loading otherwise valid PRs.
- Downgrade a true completeness flag contradicted by extraction errors or an incomplete stop reason, and record a quality issue.

These are initial policies, not comprehensive validation. Rejected records stay in original raw JSON. Issues are printed and saved as JSON text in run metadata. One rejected record can have several reasons, so the quality issue count can exceed the rejected count. Metadata issues can also exist without a rejected PR.

## Upserts and transactions

An **upsert** inserts a new key or updates its existing row:

```sql
ON CONFLICT (repository, pr_number) DO UPDATE SET ...
WHERE excluded.source_extracted_at > pull_requests.source_extracted_at
```

`excluded` means the incoming row. Only a newer extracted observation can replace a PR, preventing older files or reruns from overwriting it. This freshness rule uses extraction time, not `updated_at`. Equal extraction times leave the existing observation unchanged. Run insertion uses `ON CONFLICT (run_id) DO NOTHING`. See [DuckDB INSERT documentation](https://duckdb.org/docs/current/sql/statements/insert).

A transaction groups one snapshot's run record and all accepted PR writes: `BEGIN TRANSACTION`, then `COMMIT` on success or `ROLLBACK` on failure. Other repositories can still load after a repository-level database failure.

The summary distinguishes accepted records from actual rows inserted/updated. An unchanged rerun can accept 90 records but write zero. Stored run counts describe validation outcomes, so they remain stable on reruns.

Incomplete snapshots never trigger deletion of PRs absent from their results. The warehouse therefore contains the newest loaded observation per PR across processed snapshots, potentially observed at different times. It is not complete repository history, and even exhausted pagination is not a history of PR events.

## Query examples

Open Python using `.\.venv\Scripts\python.exe` from the project root, then:

```python
import duckdb

connection = duckdb.connect("data/processed/engineering.duckdb", read_only=True)
connection.execute("SET TimeZone = 'UTC'")
print(connection.execute("""
    SELECT repository, COUNT(*) AS stored_prs
    FROM pull_requests
    GROUP BY repository
    ORDER BY repository
""").fetchall())
print(connection.execute("""
    SELECT repository, pr_number, state,
           CAST(created_at AS VARCHAR) AS created_at_utc
    FROM pull_requests
    ORDER BY repository, pr_number DESC
    LIMIT 5
""").fetchall())
print(connection.execute("""
    SELECT repository, pagination_complete, accepted_records, rejected_records
    FROM ingestion_runs
    ORDER BY repository
""").fetchall())
connection.close()
```

Timestamp text conversion keeps these examples independent of optional Python timezone-conversion packages. The original warehouse-only environment lacked `pytz`; the currently validated project environment includes it. These SQL/text examples work in either case.

JSON preserves the complete original response. The database stores selected, validated fields in typed rows, providing SQL queries, uniqueness rules, and updates. Raw files remain the source for inspection and future reprocessing.

The loader exits with code 1 for file/metadata errors, quality issues, source extraction errors, or database failures, after attempting available valid repositories. Expected pagination incompleteness alone is not a failure. This loader does not calculate KPIs or call an AI model. Those separately implemented layers are documented in [analytics](analytics.md) and [the local Agent](local_agent.md); dashboards and cloud infrastructure remain planned.
