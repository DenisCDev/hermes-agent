"""A configured cron workdir must not silently become another directory."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent.runtime_cwd import reset_required_cron_workdir, set_required_cron_workdir
from cron import scheduler


class MissingWorkdirTests(TestCase):
    def test_resolver_rejects_missing_configured_directory(self):
        with TemporaryDirectory() as root:
            missing = Path(root) / "removed"
            with self.assertRaisesRegex(FileNotFoundError, "workdir.*no longer exists"):
                scheduler._resolve_job_workdir({"workdir": str(missing)}, "missing-dir")
            file_path = Path(root) / "file.txt"
            file_path.write_text("not a directory")
            with self.assertRaisesRegex(FileNotFoundError, "workdir.*no longer exists"):
                scheduler._resolve_job_workdir({"workdir": str(file_path)}, "missing-dir")

    def test_missing_directory_stops_every_execution_mode_before_dispatch(self):
        with TemporaryDirectory() as root:
            missing = Path(root) / "removed"
            for job_fields in (
                {"no_agent": True, "script": "task.sh"},
                {"prompt": "check the project", "script": "prepare.sh"},
                {"prompt": "check the project"},
            ):
                with self.subTest(job_fields=job_fields):
                    job = {"id": "missing-dir", "name": "Missing directory", "workdir": str(missing), **job_fields}
                    with patch.object(scheduler, "_prepare_job_prompt", side_effect=AssertionError("job dispatched")):
                        success, output, response, error = scheduler.run_job(job)
                    self.assertFalse(success)
                    self.assertIn(str(missing), output)
                    self.assertEqual(response, "")
                    self.assertIn("workdir", error)

    def test_directory_removed_during_prompt_preparation_still_fails(self):
        job = {"id": "race", "name": "Race", "workdir": "configured", "prompt": "do work"}
        error = scheduler.MissingCronWorkdirError("Cron workdir disappeared during preparation")

        def prepare(*_args):
            scheduler._resolve_job_workdir(job, job["id"])
            self.fail("prompt preparation continued after the workdir disappeared")

        with patch.object(scheduler, "_resolve_job_workdir", side_effect=["configured", error]):
            with patch.object(scheduler, "_prepare_job_prompt", side_effect=prepare):
                success, output, response, reported = scheduler.run_job(job)
        self.assertFalse(success)
        self.assertIn("workdir disappeared", output)
        self.assertEqual(response, "")
        self.assertIn("workdir disappeared", reported)

    def test_job_without_workdir_keeps_its_existing_result(self):
        result = (True, "saved output", "done", None)
        with patch.object(scheduler, "_prepare_job_prompt", return_value=(result, None)):
            self.assertEqual(scheduler.run_job({"id": "default", "name": "Default"}), result)

    def test_removed_directory_blocks_tool_cwd_resolution_during_run(self):
        from tools.code_execution_env import _resolve_child_cwd
        from tools.file_tools_paths import _resolve_base_dir
        from tools.terminal_tool import _plan_execution, _resolve_command_cwd

        with TemporaryDirectory() as root:
            workdir = Path(root) / "removed"
            workdir.mkdir()
            token = set_required_cron_workdir(str(workdir))
            try:
                _resolve_child_cwd("project", root)
                workdir.rmdir()
                for resolve in (
                    lambda: _resolve_child_cwd("project", root),
                    lambda: _resolve_base_dir(),
                    lambda: _plan_execution("pwd", task_id="cron:test:run", timeout=None,
                                            background=False, _host_local=False),
                    lambda: _resolve_command_cwd(workdir=root, default_cwd=root),
                ):
                    with self.subTest(resolve=resolve):
                        with self.assertRaisesRegex(FileNotFoundError, "Cron workdir no longer exists"):
                            resolve()
            finally:
                reset_required_cron_workdir(token)

    def test_required_workdir_scope_is_restored_after_run(self):
        with TemporaryDirectory() as root:
            missing = str(Path(root) / "missing")
            token = set_required_cron_workdir(missing)
            from agent.runtime_cwd import resolve_agent_cwd
            try:
                with self.assertRaises(FileNotFoundError):
                    resolve_agent_cwd()
            finally:
                reset_required_cron_workdir(token)
            self.assertIsInstance(resolve_agent_cwd(), Path)
