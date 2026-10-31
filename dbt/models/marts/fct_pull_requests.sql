-- One latest stored observation per repository/PR; this is not event history.
select
    pr_key, repository, pr_number, title, observed_state, author,
    created_at_utc, updated_at_utc, closed_at_utc, merged_at_utc,
    observed_at_utc, observation_run_id, observation_pagination_complete,
    observation_source_error, observation_pages_fetched,
    time_to_merge_hours, open_age_days,
    'https://github.com/' || repository || '/pull/' || cast(pr_number as varchar) as pr_url,
    'observed, potentially incomplete PR cohort' as cohort_scope
from {{ ref('int_pull_request_lifecycle') }}
