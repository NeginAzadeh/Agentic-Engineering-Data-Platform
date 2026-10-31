-- run_id alone is insufficient: a PR must belong to that run's repository.
select p.pr_key
from {{ ref('stg_github__pull_requests') }} p
left join {{ ref('stg_github__ingestion_runs') }} r
    on p.observation_run_id = r.run_id and p.repository = r.repository
where r.run_id is null or p.observed_at_utc <> r.extracted_at_utc
