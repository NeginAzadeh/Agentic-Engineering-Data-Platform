-- Formulas mirror sql/analytics/repository_kpis.sql; parity is checked independently.
-- Aggregate PRs before joining run metadata, avoiding one-to-many count inflation.
WITH pr_metrics AS (
    SELECT
        repository,
        COUNT(*) AS total_observed_prs,
        SUM(CASE WHEN observed_state = 'open' THEN 1 ELSE 0 END) AS open_prs,
        SUM(CASE WHEN merged_at_utc IS NOT NULL THEN 1 ELSE 0 END) AS merged_prs,
        SUM(CASE WHEN observed_state = 'closed' AND merged_at_utc IS NULL THEN 1 ELSE 0 END)
            AS closed_without_merge_prs,
        AVG(time_to_merge_hours) AS average_time_to_merge_hours,
        MEDIAN(time_to_merge_hours) AS median_time_to_merge_hours,
        COUNT(time_to_merge_hours) AS merge_duration_sample_count,
        AVG(open_age_days) AS average_open_age_days,
        MEDIAN(open_age_days) AS median_open_age_days,
        COUNT(open_age_days) AS open_age_sample_count,
        SUM(CASE WHEN open_age_days > 7.0 THEN 1 ELSE 0 END) AS open_prs_older_than_7_days,
        MIN(observed_at_utc) AS earliest_pr_extraction_at,
        MAX(observed_at_utc) AS latest_pr_extraction_at,
        LIST(DISTINCT observation_run_id ORDER BY observation_run_id) AS observation_run_ids,
        CASE
            WHEN SUM(CASE WHEN observation_pagination_complete = FALSE THEN 1 ELSE 0 END) > 0
                THEN FALSE
            WHEN COUNT(observation_pagination_complete) < COUNT(*) THEN NULL
            ELSE TRUE
        END AS all_observation_runs_pagination_complete,
        SUM(CASE WHEN observation_source_error IS NOT NULL THEN 1 ELSE 0 END)
            AS prs_from_runs_with_source_errors
    FROM {{ ref('fct_pull_requests') }}
    GROUP BY repository
), ranked_runs AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY repository ORDER BY extracted_at_utc DESC, run_id ASC
    ) AS run_rank
    FROM {{ ref('stg_github__ingestion_runs') }}
), repositories AS (
    -- Include repositories with an extraction run but no accepted PRs.
    SELECT repository FROM {{ ref('stg_github__ingestion_runs') }}
    UNION
    SELECT repository FROM {{ ref('fct_pull_requests') }}
)
SELECT
    repositories.repository,
    COALESCE(m.total_observed_prs, 0) AS total_observed_prs,
    COALESCE(m.open_prs, 0) AS open_prs,
    COALESCE(m.merged_prs, 0) AS merged_prs,
    COALESCE(m.closed_without_merge_prs, 0) AS closed_without_merge_prs,
    m.merged_prs * 1.0 / NULLIF(m.total_observed_prs, 0) AS merge_rate,
    m.average_time_to_merge_hours,
    m.median_time_to_merge_hours,
    COALESCE(m.merge_duration_sample_count, 0) AS merge_duration_sample_count,
    m.average_open_age_days,
    m.median_open_age_days,
    COALESCE(m.open_age_sample_count, 0) AS open_age_sample_count,
    COALESCE(m.open_prs_older_than_7_days, 0) AS open_prs_older_than_7_days,
    strftime(m.earliest_pr_extraction_at, '%Y-%m-%dT%H:%M:%S.%fZ') AS earliest_pr_extraction_at_utc,
    strftime(m.latest_pr_extraction_at, '%Y-%m-%dT%H:%M:%S.%fZ') AS latest_pr_extraction_at_utc,
    m.observation_run_ids,
    m.all_observation_runs_pagination_complete,
    COALESCE(m.prs_from_runs_with_source_errors, 0) AS prs_from_runs_with_source_errors,
    r.run_id AS latest_run_id,
    strftime(r.extracted_at_utc, '%Y-%m-%dT%H:%M:%S.%fZ') AS latest_run_extracted_at_utc,
    r.pagination_complete AS latest_run_pagination_complete,
    r.stop_reason AS latest_run_stop_reason,
    r.source_error AS latest_run_source_error,
    r.accepted_records AS latest_run_accepted_records,
    r.rejected_records AS latest_run_rejected_records,
    r.quality_issues AS latest_run_quality_issues,
    'observed, potentially incomplete PR cohort' AS cohort_scope
FROM repositories
LEFT JOIN pr_metrics AS m USING (repository)
LEFT JOIN ranked_runs AS r ON repositories.repository = r.repository AND r.run_rank = 1
ORDER BY repositories.repository
