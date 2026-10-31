select
    repository || '#' || cast(pr_number as varchar) as pr_key,
    repository,
    pr_number,
    title,
    state as observed_state,
    author,
    created_at as created_at_utc,
    updated_at as updated_at_utc,
    closed_at as closed_at_utc,
    merged_at as merged_at_utc,
    source_extracted_at as observed_at_utc,
    last_run_id as observation_run_id
from {{ source('engineering_warehouse', 'pull_requests') }}
