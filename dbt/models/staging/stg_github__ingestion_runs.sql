select
    run_id,
    repository,
    extracted_at as extracted_at_utc,
    finished_at as finished_at_utc,
    source_url,
    snapshot_file,
    pagination_complete,
    pages_fetched,
    stop_reason,
    source_error,
    record_count,
    accepted_records,
    rejected_records,
    quality_issues
from {{ source('engineering_warehouse', 'ingestion_runs') }}
