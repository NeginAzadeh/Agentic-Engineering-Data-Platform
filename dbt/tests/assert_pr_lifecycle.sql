-- Returning any row fails the test; do not silently repair invalid observations.
select pr_key
from {{ ref('fct_pull_requests') }}
where pr_number <= 0
   or updated_at_utc < created_at_utc
   or closed_at_utc < created_at_utc
   or merged_at_utc < created_at_utc
   or closed_at_utc > updated_at_utc
   or merged_at_utc > updated_at_utc
   or merged_at_utc > closed_at_utc
   or (observed_state = 'open' and (closed_at_utc is not null or merged_at_utc is not null))
   or (observed_state = 'closed' and closed_at_utc is null)
   or (observed_state = 'open' and observed_at_utc < created_at_utc)
