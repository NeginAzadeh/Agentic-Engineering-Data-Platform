#!/usr/bin/env bash
# Source this file in each WSL terminal before using Airflow.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo 'Use: source orchestration/environment.sh' >&2
    exit 1
fi
if [[ "$(uname -s)" != 'Linux' ]]; then
    echo 'This runtime requires Linux (WSL2 on Windows).' >&2
    return 1
fi

export ENGINEERING_PROJECT_ROOT
ENGINEERING_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)" || return 1
export ENGINEERING_PIPELINE_PYTHON="$HOME/.venvs/aedp-pipeline/bin/python"
export AIRFLOW_HOME="$HOME/.local/share/aedp-airflow"
export PATH="$HOME/.venvs/aedp-airflow/bin:$PATH"
export PYTHONPATH="$ENGINEERING_PROJECT_ROOT"
export DBT_SEND_ANONYMOUS_USAGE_STATS=false
export AIRFLOW__CORE__DAGS_FOLDER="$ENGINEERING_PROJECT_ROOT/orchestration/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=false
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
export AIRFLOW__CORE__PARALLELISM=1
export AIRFLOW__CORE__MAX_ACTIVE_TASKS_PER_DAG=1
export AIRFLOW__CORE__MAX_ACTIVE_RUNS_PER_DAG=1
export AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=true
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///$AIRFLOW_HOME/airflow.db"
export AIRFLOW__DAG_PROCESSOR__PARSING_PROCESSES=1
export AIRFLOW__API__HOST=127.0.0.1
export AIRFLOW__API__WORKERS=1
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=false
