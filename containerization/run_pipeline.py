"""Run the fixed four-stage pipeline in one container using existing helpers."""

import os
import signal
import subprocess
import sys
from pathlib import Path

from orchestration.pipeline_runtime import (
    STAGES, STAGE_TIMEOUTS, command_for, execute_command, get_runtime, pipeline_lock,
)


def require_data_mount(project):
    """Refuse accidental runs without explicit persistent container storage."""
    data = project / 'data'
    mounted = any(line.split()[4] == str(data)
                  for line in Path('/proc/self/mountinfo').read_text().splitlines())
    if not mounted or not data.is_dir():
        raise ValueError(f'{data} must be an explicit data mount; use compose.yaml.')
    if os.geteuid() == 0:
        raise ValueError('Run the pipeline as a non-root UID/GID with access to the data mount.')


def run_pipeline():
    runtime = get_runtime()
    require_data_mount(runtime.project)
    # Hold the SAME cooperative lock used by Airflow across the entire run.
    # Call its command/execution helpers directly to avoid nested stage locks.
    with pipeline_lock(runtime.project) as lock_fd:
        for stage in STAGES:
            print(f'Starting {stage}', flush=True)
            execute_command(command_for(stage, runtime), runtime.project,
                            dict(os.environ), STAGE_TIMEOUTS[stage], lock_fd)
            print(f'Completed {stage}', flush=True)


def terminate(signum, frame):
    # execute_command catches BaseException and cleans up its process group.
    raise SystemExit(128 + signum)


def main(arguments=None):
    if arguments is None:
        arguments = sys.argv[1:]
    if arguments:
        print('This entry point accepts no commands or arguments.', file=sys.stderr)
        return 2
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        run_pipeline()
    except subprocess.TimeoutExpired as error:
        print(f'Pipeline timed out: {error}', file=sys.stderr)
        return 124
    except subprocess.CalledProcessError as error:
        print(f'Pipeline stage failed with exit code {error.returncode}: {error.cmd!r}', file=sys.stderr)
        return error.returncode if 0 < error.returncode < 126 else 1
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Pipeline failed: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
