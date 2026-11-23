"""Offline tests for the Docker entry point and its safety boundaries."""

import signal
import subprocess
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from containerization import run_pipeline as entry


class ContainerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(project=Path('/app'), python=Path('/opt/venv/bin/python'))

    def test_fixed_stage_order_uses_shared_timeouts_and_one_run_wide_lock(self):
        events = []

        @contextmanager
        def lock(project):
            self.assertEqual(project, self.runtime.project)
            events.append('locked')
            yield 42
            events.append('released')

        def execute(command, project, env, timeout, lock_fd):
            self.assertEqual(project, self.runtime.project)
            self.assertEqual(lock_fd, 42)
            self.assertEqual(timeout, entry.STAGE_TIMEOUTS[command[0]])
            events.append(command[0])

        with patch.object(entry, 'get_runtime', return_value=self.runtime), \
                patch.object(entry, 'require_data_mount') as mounted, \
                patch.object(entry, 'pipeline_lock', lock), \
                patch.object(entry, 'command_for', side_effect=lambda stage, runtime: [stage]), \
                patch.object(entry, 'execute_command', side_effect=execute), redirect_stdout(StringIO()):
            entry.run_pipeline()
        mounted.assert_called_once_with(self.runtime.project)
        self.assertEqual(events, ['locked', 'ingest_github', 'load_warehouse',
                                  'dbt_build', 'verify_kpis', 'released'])

    def test_each_failure_blocks_all_downstream_stages_and_releases_lock(self):
        stages = ['ingest_github', 'load_warehouse', 'dbt_build', 'verify_kpis']
        for index, failed in enumerate(stages):
            events = []

            @contextmanager
            def lock(project):
                try:
                    yield 42
                finally:
                    events.append('released')

            def execute(command, *args):
                events.append(command[0])
                if command[0] == failed:
                    raise subprocess.CalledProcessError(7, command)

            with self.subTest(stage=failed), \
                    patch.object(entry, 'get_runtime', return_value=self.runtime), \
                    patch.object(entry, 'require_data_mount'), \
                    patch.object(entry, 'pipeline_lock', lock), \
                    patch.object(entry, 'command_for', side_effect=lambda stage, runtime: [stage]), \
                    patch.object(entry, 'execute_command', side_effect=execute), redirect_stdout(StringIO()):
                with self.assertRaises(subprocess.CalledProcessError):
                    entry.run_pipeline()
            self.assertEqual(events, stages[:index + 1] + ['released'])

    def test_missing_mount_fails_before_starting_commands(self):
        with patch.object(entry, 'get_runtime', return_value=self.runtime), \
                patch.object(entry, 'require_data_mount', side_effect=ValueError('missing mount')), \
                patch.object(entry, 'execute_command') as execute:
            with self.assertRaisesRegex(ValueError, 'missing mount'):
                entry.run_pipeline()
        execute.assert_not_called()

    def test_busy_lock_never_starts_a_stage(self):
        with patch.object(entry, 'get_runtime', return_value=self.runtime), \
                patch.object(entry, 'require_data_mount'), \
                patch.object(entry, 'pipeline_lock', side_effect=RuntimeError('busy')), \
                patch.object(entry, 'execute_command') as execute:
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                entry.run_pipeline()
        execute.assert_not_called()

    def test_mount_check_rejects_unmounted_data_and_root(self):
        mount = f"1 2 0:1 / {Path('/app') / 'data'} rw - ext4 /dev/test rw\n"
        with patch.object(Path, 'read_text', return_value=''), \
                patch.object(Path, 'is_dir', return_value=True):
            with self.assertRaisesRegex(ValueError, 'explicit data mount'):
                entry.require_data_mount(Path('/app'))
        with patch.object(Path, 'read_text', return_value=mount), \
                patch.object(Path, 'is_dir', return_value=True), \
                patch.object(entry.os, 'geteuid', return_value=0, create=True):
            with self.assertRaisesRegex(ValueError, 'non-root'):
                entry.require_data_mount(Path('/app'))
        with patch.object(Path, 'read_text', return_value=mount), \
                patch.object(Path, 'is_dir', return_value=True), \
                patch.object(entry.os, 'geteuid', return_value=1000, create=True):
            entry.require_data_mount(Path('/app'))

    def test_cli_rejects_commands_before_execution(self):
        with patch.object(entry, 'run_pipeline') as execute, redirect_stderr(StringIO()):
            self.assertEqual(entry.main(['arbitrary-command']), 2)
        execute.assert_not_called()

    def test_cli_exit_codes_and_signal_handler_restoration(self):
        cases = [(None, 0), (subprocess.CalledProcessError(7, ['dbt']), 7),
                 (subprocess.TimeoutExpired(['dbt'], 600), 124),
                 (ValueError('configuration'), 1), (KeyboardInterrupt(), 130)]
        for error, expected in cases:
            with self.subTest(error=error), \
                    patch.object(entry, 'run_pipeline', side_effect=error), \
                    patch.object(entry.signal, 'signal', return_value='previous') as handler, \
                    redirect_stderr(StringIO()):
                self.assertEqual(entry.main([]), expected)
                self.assertEqual(handler.call_args_list[-1].args, (signal.SIGTERM, 'previous'))

    def test_sigterm_has_meaningful_exit_code(self):
        with self.assertRaises(SystemExit) as result:
            entry.terminate(signal.SIGTERM, None)
        self.assertEqual(result.exception.code, 143)


if __name__ == '__main__':
    unittest.main()
