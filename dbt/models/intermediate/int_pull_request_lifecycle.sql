select
    p.*,
    r.pagination_complete as observation_pagination_complete,
    r.source_error as observation_source_error,
    r.pages_fetched as observation_pages_fetched,
    case
        when p.merged_at_utc is not null and p.merged_at_utc >= p.created_at_utc
        then (epoch(p.merged_at_utc) - epoch(p.created_at_utc)) / 3600.0
    end as time_to_merge_hours,
    case
        when p.observed_state = 'open' and p.observed_at_utc >= p.created_at_utc
        then (epoch(p.observed_at_utc) - epoch(p.created_at_utc)) / 86400.0
    end as open_age_days
from {{ ref('stg_github__pull_requests') }} p
left join {{ ref('stg_github__ingestion_runs') }} r
    on p.observation_run_id = r.run_id and p.repository = r.repository
