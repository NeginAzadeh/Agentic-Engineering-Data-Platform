"""Manual, serial orchestration of the existing observed-cohort pipeline."""

from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task

from orchestration.pipeline_runtime import STAGE_TIMEOUTS, run_stage


with DAG(
    dag_id='engineering_data_pipeline',
    description='GitHub snapshots -> validated warehouse -> dbt build -> KPI parity',
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    schedule=None,
    catchup=False,
    is_paused_upon_creation=True,
    max_active_runs=1,
    max_active_tasks=1,
    dagrun_timeout=timedelta(minutes=30),
    default_args={'retries': 0, 'retry_delay': timedelta(minutes=5),
                  'trigger_rule': 'all_success'},
    tags=['local', 'engineering', 'observed-cohort'],
) as dag:

    @task(task_id='ingest_github', do_xcom_push=False,
          execution_timeout=timedelta(seconds=STAGE_TIMEOUTS['ingest_github'] + 30))
    def ingest_github():
        run_stage('ingest_github')

    @task(task_id='load_warehouse', do_xcom_push=False,
          execution_timeout=timedelta(seconds=STAGE_TIMEOUTS['load_warehouse'] + 30))
    def load_warehouse():
        run_stage('load_warehouse')

    @task(task_id='dbt_build', do_xcom_push=False,
          execution_timeout=timedelta(seconds=STAGE_TIMEOUTS['dbt_build'] + 30))
    def dbt_build():
        run_stage('dbt_build')

    @task(task_id='verify_kpis', do_xcom_push=False,
          execution_timeout=timedelta(seconds=STAGE_TIMEOUTS['verify_kpis'] + 30))
    def verify_kpis():
        run_stage('verify_kpis')

    ingest_github() >> load_warehouse() >> dbt_build() >> verify_kpis()
