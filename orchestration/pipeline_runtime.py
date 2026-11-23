"""Thin Linux command runner for existing pipeline stages; no business logic."""

import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


# Process budgets; Airflow's task budgets include another 30 seconds for cleanup.
STAGE_TIMEOUTS = {
    'ingest_github': 300,
    'load_warehouse': 180,
    'dbt_build': 600,
    'verify_kpis': 120,
}
STAGES = tuple(STAGE_TIMEOUTS)
PROJECT_FILES = (
    'src/engineering_intelligence/ingest_github.py',
    'src/engineering_intelligence/load_github.py',
    'src/engineering_intelligence/verify_dbt_kpis.py',
    'dbt/dbt_project.yml',
    'dbt/profiles.yml',
)


@dataclass(frozen=True)
class Runtime:
    project: Path
    python: Path


def require_linux_path(name, value):
    if (not value or not PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).drive or '\\' in value):
        raise ValueError(f'{name} must be an absolute Linux path; convert Windows paths with wslpath -u.')
    return value


def validate_project(project):
    missing = [name for name in PROJECT_FILES if not (project / name).is_file()]
    if missing:
        raise ValueError(f'Project files missing from {project}: {", ".join(missing)}')


def get_runtime():
    if sys.platform != 'linux':
        raise RuntimeError('Airflow tasks require Linux. Use WSL2; do not use the Windows .venv.')
    project = Path(require_linux_path('ENGINEERING_PROJECT_ROOT', os.getenv('ENGINEERING_PROJECT_ROOT'))).resolve(strict=True)
    python = Path(require_linux_path('ENGINEERING_PIPELINE_PYTHON', os.getenv('ENGINEERING_PIPELINE_PYTHON')))
    validate_project(project)
    if project != Path(__file__).resolve().parents[1]:
        raise ValueError('The configured project must be the checkout containing this runner.')
    # Keep the venv's bin/python path: resolving its symlink would bypass the venv.
    if (python.parent.name != 'bin' or not (python.parent.parent / 'pyvenv.cfg').is_file()
            or not python.is_file() or not os.access(python, os.X_OK)):
        raise ValueError('ENGINEERING_PIPELINE_PYTHON must be an executable Linux venv bin/python.')
    if not (python.parent / 'dbt').is_file():
        raise ValueError('dbt is missing from the Linux pipeline venv; install requirements-pipeline.txt.')
    return Runtime(project, python)


def command_for(stage, runtime):
    modules = {
        'ingest_github': 'src.engineering_intelligence.ingest_github',
        'load_warehouse': 'src.engineering_intelligence.load_github',
        'verify_kpis': 'src.engineering_intelligence.verify_dbt_kpis',
    }
    if stage in modules:
        return [str(runtime.python), '-m', modules[stage]]
    if stage == 'dbt_build':
        return [str(runtime.python.parent / 'dbt'), 'build',
                '--project-dir', str(runtime.project / 'dbt'),
                '--profiles-dir', str(runtime.project / 'dbt')]
    raise ValueError(f'Unknown pipeline stage: {stage}')


@contextmanager
def pipeline_lock(project):
    """Cooperative, nonblocking Linux lock shared by all four stage commands."""
    import fcntl  # Linux standard library; no Windows Airflow installation needed.

    processed = project / 'data/processed'
    processed.mkdir(parents=True, exist_ok=True)
    with (processed / '.airflow_pipeline.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another pipeline stage holds the DuckDB access lock.') from error
        # Pass the lock to the child too: a killed task cannot release protection
        # while its direct child is still alive. The lock file is never unlinked.
        os.set_inheritable(lock.fileno(), True)
        try:
            yield lock.fileno()
        finally:
            os.set_inheritable(lock.fileno(), False)
            # Closing the file releases the lock once all inherited copies close.


def stop_process_group(process):
    """Terminate the command and its children before releasing its file lock."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The direct child can exit before its descendants; signal the whole group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def execute_command(command, project, env, timeout, lock_fd):
    # Inherit output so Airflow captures the real scripts' logs without buffering
    # a second copy or putting raw data into XCom. No shell interprets arguments.
    process = subprocess.Popen(command, cwd=project, env=env, shell=False,
                               start_new_session=True, pass_fds=(lock_fd,))
    try:
        returncode = process.wait(timeout=timeout)
    except BaseException:
        # Includes an Airflow timeout/termination as well as the process timeout.
        stop_process_group(process)
        raise
    if returncode:
        stop_process_group(process)
        raise subprocess.CalledProcessError(returncode, command)


def run_stage(stage):
    if stage not in STAGE_TIMEOUTS:
        raise ValueError(f'Unknown pipeline stage: {stage}')
    runtime = get_runtime()
    command = command_for(stage, runtime)
    env = dict(os.environ)
    env.pop('PYTHONHOME', None)
    env.update(ENGINEERING_PROJECT_ROOT=str(runtime.project),
               VIRTUAL_ENV=str(runtime.python.parent.parent),
               PATH=str(runtime.python.parent) + os.pathsep + env.get('PATH', ''),
               PYTHONPATH=str(runtime.project), PYTHONUNBUFFERED='1',
               DBT_SEND_ANONYMOUS_USAGE_STATS='false')
    with pipeline_lock(runtime.project) as lock_fd:
        print(f'Starting {stage}: {command!r}', flush=True)
        execute_command(command, runtime.project, env, STAGE_TIMEOUTS[stage], lock_fd)
        print(f'Completed {stage} successfully.', flush=True)
