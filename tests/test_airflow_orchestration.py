"""Offline DAG contracts and command boundaries; Airflow is mocked on Windows."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from orchestration import pipeline_runtime as runner


ROOT = Path(__file__).resolve().parents[1]
DAG_FILE = ROOT / 'orchestration/dags/engineering_pipeline.py'


def load_with_mock_sdk():
    """Read the real DAG declaration without pretending to import Airflow itself."""
    dags = []

    class Dag:
        def __init__(self, **kwargs):
            self.config = kwargs
            self.nodes = {}
            dags.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Node:
        def __init__(self, function, options):
            self.function, self.options = function, options
            self.upstream, self.downstream = set(), set()
            self.task_id = options['task_id']
            dags[-1].nodes[self.task_id] = self

        def __rshift__(self, other):
            self.downstream.add(other.task_id)
            other.upstream.add(self.task_id)
            return other

    def task(**options):
        def decorate(function):
            return lambda: Node(function, options)
        return decorate

    airflow = types.ModuleType('airflow')
    sdk = types.ModuleType('airflow.sdk')
    sdk.DAG, sdk.task = Dag, task
    spec = importlib.util.spec_from_file_location('engineering_pipeline_offline_test', DAG_FILE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'airflow': airflow, 'airflow.sdk': sdk}):
        spec.loader.exec_module(module)
    return module, dags[0]


class DagOfflineTests(unittest.TestCase):
    def setUp(self):
        with patch.object(runner, 'run_stage') as execute:
            self.module, self.dag = load_with_mock_sdk()
            execute.assert_not_called()  # parsing the DAG must never run ingestion

    def test_manual_serial_dag_has_bounded_timeouts_and_no_catchup(self):
        config = self.dag.config
        self.assertEqual(config['dag_id'], 'engineering_data_pipeline')
        self.assertIsNone(config['schedule'])
        self.assertFalse(config['catchup'])
        self.assertTrue(config['is_paused_upon_creation'])
        self.assertEqual(config['max_active_runs'], 1)
        self.assertEqual(config['max_active_tasks'], 1)
        self.assertEqual(config['default_args']['retries'], 0)
        self.assertEqual(config['default_args']['trigger_rule'], 'all_success')
        self.assertEqual(config['dagrun_timeout'], timedelta(minutes=30))
        self.assertIsNotNone(config['start_date'].tzinfo)
        for node in self.dag.nodes.values():
            self.assertFalse(node.options['do_xcom_push'])
            self.assertEqual(node.options['execution_timeout'].total_seconds(),
                             runner.STAGE_TIMEOUTS[node.task_id] + 30)

    def test_exact_dependency_order_blocks_downstream_without_upstream_success(self):
        expected = ['ingest_github', 'load_warehouse', 'dbt_build', 'verify_kpis']
        self.assertEqual(set(self.dag.nodes), set(expected))
        for index, name in enumerate(expected):
            node = self.dag.nodes[name]
            self.assertEqual(node.upstream, {expected[index - 1]} if index else set())
            self.assertEqual(node.downstream, {expected[index + 1]} if index < 3 else set())
            # No all_done leaf can mask a validation failure.
            self.assertEqual(self.dag.config['default_args']['trigger_rule'], 'all_success')

    def test_tasks_call_existing_stage_runner_and_propagate_every_failure(self):
        for name, node in self.dag.nodes.items():
            with self.subTest(stage=name):
                failure = subprocess.CalledProcessError(1, ['existing-script'])
                with patch.object(self.module, 'run_stage', side_effect=failure) as execute:
                    with self.assertRaises(subprocess.CalledProcessError):
                        node.function()
                    execute.assert_called_once_with(name)


class PipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='airflow project ')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        # Deliberately different from the Airflow interpreter.
        self.runtime = runner.Runtime(self.project, self.project / 'pipeline venv/bin/python')
        self.addCleanup(patch.stopall)
        patch.object(runner, 'get_runtime', return_value=self.runtime).start()
        self.stdout = patch('sys.stdout', new_callable=StringIO).start()

    def test_fixed_argv_uses_pipeline_venv_and_existing_modules_without_shell(self):
        for stage, module in [
            ('ingest_github', 'src.engineering_intelligence.ingest_github'),
            ('load_warehouse', 'src.engineering_intelligence.load_github'),
            ('verify_kpis', 'src.engineering_intelligence.verify_dbt_kpis'),
        ]:
            self.assertEqual(runner.command_for(stage, self.runtime),
                             [str(self.runtime.python), '-m', module])
        self.assertEqual(runner.command_for('dbt_build', self.runtime),
                         [str(self.runtime.python.parent / 'dbt'), 'build',
                          '--project-dir', str(self.project / 'dbt'),
                          '--profiles-dir', str(self.project / 'dbt')])

    def test_runner_acquires_lock_and_passes_correct_environment_and_budget(self):
        events = []

        @contextmanager
        def lock(project):
            self.assertEqual(project, self.project)
            events.append('locked')
            yield 42
            events.append('released')

        def execute(command, project, env, timeout, lock_fd):
            self.assertEqual(events, ['locked'])
            self.assertEqual(command[0], str(self.runtime.python))
            self.assertEqual(project, self.project)
            self.assertEqual(timeout, 300)
            self.assertEqual(lock_fd, 42)
            self.assertEqual(env['ENGINEERING_PROJECT_ROOT'], str(self.project))
            self.assertEqual(env['DBT_SEND_ANONYMOUS_USAGE_STATS'], 'false')
            self.assertEqual(env['PYTHONPATH'], str(self.project))
            self.assertEqual(env['VIRTUAL_ENV'], str(self.runtime.python.parent.parent))
            self.assertNotIn('PYTHONHOME', env)
            events.append('executed')

        with patch.dict(os.environ, {'PYTHONHOME': '/wrong', 'PYTHONPATH': '/wrong'}), \
                patch.object(runner, 'pipeline_lock', lock), \
                patch.object(runner, 'execute_command', side_effect=execute):
            runner.run_stage('ingest_github')
        self.assertEqual(events, ['locked', 'executed', 'released'])

    def test_busy_lock_never_starts_command(self):
        with patch.object(runner, 'pipeline_lock', side_effect=RuntimeError('busy')), \
                patch.object(runner, 'execute_command') as execute:
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                runner.run_stage('dbt_build')
            execute.assert_not_called()

    def test_nonzero_exit_remains_a_failure_and_releases_lock(self):
        lock = Mock()
        lock.__enter__ = Mock(return_value=42)
        lock.__exit__ = Mock(return_value=False)
        with patch.object(runner, 'pipeline_lock', return_value=lock), \
                patch.object(runner, 'execute_command', side_effect=subprocess.CalledProcessError(1, ['dbt'])):
            with self.assertRaises(subprocess.CalledProcessError):
                runner.run_stage('dbt_build')
        lock.__exit__.assert_called_once()
        self.assertNotIn('successfully', self.stdout.getvalue())

    def test_unknown_stage_never_starts_process(self):
        with patch.object(runner, 'execute_command') as execute:
            with self.assertRaises(ValueError):
                runner.run_stage('arbitrary_command')
            execute.assert_not_called()

    def test_process_execution_streams_logs_and_checks_exit_status(self):
        process = Mock()
        process.wait.return_value = 0
        with patch.object(runner.subprocess, 'Popen', return_value=process) as popen:
            runner.execute_command(['python', '-m', 'existing'], self.project, {'A': 'B'}, 30, 42)
        popen.assert_called_once_with(['python', '-m', 'existing'], cwd=self.project,
                                      env={'A': 'B'}, shell=False,
                                      start_new_session=True, pass_fds=(42,))
        process.wait.assert_called_once_with(timeout=30)

    def test_nonzero_process_exit_cleans_up_and_raises(self):
        process = Mock()
        process.wait.return_value = 7
        with patch.object(runner.subprocess, 'Popen', return_value=process), \
                patch.object(runner, 'stop_process_group') as stop:
            with self.assertRaises(subprocess.CalledProcessError) as error:
                runner.execute_command(['dbt', 'build'], self.project, {}, 30, 42)
        self.assertEqual(error.exception.returncode, 7)
        stop.assert_called_once_with(process)

    def test_process_timeout_or_task_termination_cleans_up_and_propagates(self):
        for error in (subprocess.TimeoutExpired(['dbt'], 30), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                process = Mock()
                process.wait.side_effect = error
                with patch.object(runner.subprocess, 'Popen', return_value=process), \
                        patch.object(runner, 'stop_process_group') as stop:
                    with self.assertRaises(type(error)):
                        runner.execute_command(['dbt'], self.project, {}, 30, 42)
                stop.assert_called_once_with(process)

    def test_process_group_cleanup_escalates_to_kill(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired(['dbt'], 5), 0]
        with patch.object(runner.os, 'killpg', create=True) as killpg, \
                patch.object(runner.signal, 'SIGKILL', 9, create=True):
            runner.stop_process_group(process)
            self.assertEqual(killpg.call_args_list[-1].args[1], runner.signal.SIGKILL)
        self.assertEqual([call.args[0] for call in killpg.call_args_list], [12345, 12345])


class RuntimePreflightTests(unittest.TestCase):
    def test_windows_and_relative_paths_are_rejected(self):
        for value in (None, '', 'relative/project', 'C:\\project', 'C:/project', '//server/share'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.require_linux_path('project', value)
        self.assertEqual(runner.require_linux_path('project', '/mnt/c/Users/Example/project with spaces'),
                         '/mnt/c/Users/Example/project with spaces')

    def test_native_windows_is_rejected_before_any_stage_executes(self):
        with patch.object(runner.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(RuntimeError, 'WSL2'):
                runner.get_runtime()

    def test_project_validation_checks_real_required_files(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'missing'):
                runner.validate_project(Path(temp))
        runner.validate_project(ROOT)

    def test_lock_is_nonblocking_and_marks_child_inheritance(self):
        fake = types.ModuleType('fcntl')
        fake.LOCK_EX, fake.LOCK_NB = 1, 2
        fake.flock = Mock()
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, {'fcntl': fake}), \
                patch.object(runner.os, 'set_inheritable') as inherit:
            with runner.pipeline_lock(Path(temp)) as fd:
                fake.flock.assert_called_once_with(fd, fake.LOCK_EX | fake.LOCK_NB)
            self.assertEqual([c.args for c in inherit.call_args_list], [(fd, True), (fd, False)])
            self.assertTrue((Path(temp) / 'data/processed/.airflow_pipeline.lock').is_file())

    def test_contended_kernel_lock_is_reported(self):
        fake = types.ModuleType('fcntl')
        fake.LOCK_EX, fake.LOCK_NB = 1, 2
        fake.flock = Mock(side_effect=BlockingIOError())
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {'fcntl': fake}):
            with self.assertRaisesRegex(RuntimeError, 'Another pipeline'):
                with runner.pipeline_lock(Path(temp)):
                    self.fail('Contended lock must never enter the protected section')


@unittest.skipUnless(sys.platform == 'linux' and importlib.util.find_spec('airflow') is not None,
                     'Real Airflow DAG import requires the separate Linux Airflow environment')
class AirflowNativeImportTests(unittest.TestCase):
    def test_actual_airflow_dagbag_accepts_dag_and_dependencies(self):
        from airflow.dag_processing.dagbag import DagBag

        bag = DagBag(dag_folder=str(DAG_FILE.parent))
        self.assertFalse(bag.import_errors, bag.import_errors)
        dag = bag.dags['engineering_data_pipeline']
        self.assertEqual(dag.max_active_runs, 1)
        self.assertEqual(dag.max_active_tasks, 1)
        self.assertFalse(dag.catchup)
        self.assertIsNone(dag.schedule)
        for upstream, downstream in zip(runner.STAGES, runner.STAGES[1:]):
            self.assertEqual(dag.task_dict[upstream].downstream_task_ids, {downstream})
        self.assertTrue(all(task.trigger_rule.value == 'all_success' for task in dag.tasks))
