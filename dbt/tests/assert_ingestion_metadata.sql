select run_id
from {{ ref('stg_github__ingestion_runs') }}
where record_count < 0 or accepted_records < 0 or rejected_records < 0
   or record_count <> accepted_records + rejected_records
   or pages_fetched < 0
   or finished_at_utc < extracted_at_utc
   or (pagination_complete and (
       source_error is not null or stop_reason in ('failed', 'partial_failure', 'page_limit', 'empty_page')
   ))
