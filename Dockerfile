FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENGINEERING_PROJECT_ROOT=/app \
    ENGINEERING_PIPELINE_PYTHON=/opt/venv/bin/python \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    DBT_TARGET_PATH=/app/data/dbt/target \
    DBT_LOG_PATH=/app/data/dbt/logs \
    HOME=/tmp

WORKDIR /app
COPY requirements-dbt.txt ./
COPY orchestration/requirements-pipeline.txt ./orchestration/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir -r orchestration/requirements-pipeline.txt \
    && /opt/venv/bin/python -m pip check \
    && groupadd --gid 1000 pipeline \
    && useradd --uid 1000 --gid 1000 --no-create-home pipeline

COPY src/engineering_intelligence/ingest_github.py src/engineering_intelligence/load_github.py src/engineering_intelligence/verify_dbt_kpis.py ./src/engineering_intelligence/
COPY orchestration/pipeline_runtime.py ./orchestration/
COPY sql/analytics/ ./sql/analytics/
COPY dbt/ ./dbt/
COPY containerization/run_pipeline.py ./containerization/

USER 1000:1000
ENTRYPOINT ["/opt/venv/bin/python", "-m", "containerization.run_pipeline"]
