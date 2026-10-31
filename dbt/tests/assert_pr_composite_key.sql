select repository, pr_number, count(*) as duplicate_count
from {{ ref('fct_pull_requests') }}
group by repository, pr_number
having count(*) > 1
