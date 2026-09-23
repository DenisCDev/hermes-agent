"""Scheduled jobs must never switch workspaces after their workdir disappears."""

from pathlib import Path
import os
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent.runtime_cwd import reset_required_cron_workdir, set_required_cron_workdir
from cron import scheduler


class MissingWorkdirTests(TestCase):
    def test_missing_workdir_blocks_script_and_agent_jobs_before_dispatch(self):
        with TemporaryDirectory() as root:
            missing = str(Path(root) / "removed")
            for fields in ({"no_agent": True, "script": "task.sh"}, {"prompt": "do work"}):
                with self.subTest(fields=fields):
                    job = {"id": "missing", "name": "Missing", "workdir": missing, **fields}
                    with patch.object(scheduler, "_run_job_script_with_claim_heartbeat",
                                      side_effect=AssertionError("script dispatched")):
                        result = scheduler.run_job(job)
                    self.assertFalse(result[0])
                    self.assertIn(missing, result[1])
                    self.assertEqual(result[2], "")
                    self.assertIn("workdir", result[3])

    def test_removed_workdir_blocks_relative_tool_paths_and_explicit_terminal_parent(self):
        from tools.code_execution_tool import _resolve_child_cwd
        from tools.file_tools import _resolve_base_dir
        from tools.terminal_tool import _resolve_command_cwd

        with TemporaryDirectory() as root:
            workdir = Path(root) / "removed"
            workdir.mkdir()
            token = set_required_cron_workdir(str(workdir))
            try:
                workdir.rmdir()
                for resolve in (
                    lambda: _resolve_child_cwd("project", root),
                    lambda: _resolve_base_dir(),
                    lambda: _resolve_command_cwd(workdir=root, default_cwd=root),
                ):
                    with self.subTest(resolve=resolve):
                        with self.assertRaisesRegex(FileNotFoundError, "Cron workdir no longer exists"):
                            resolve()
            finally:
                reset_required_cron_workdir(token)

    def test_scope_reset_restores_default_cwd_resolution(self):
        from agent.runtime_cwd import resolve_agent_cwd

        with TemporaryDirectory() as root:
            token = set_required_cron_workdir(str(Path(root) / "missing"))
            try:
                with self.assertRaises(FileNotFoundError):
                    resolve_agent_cwd()
            finally:
                reset_required_cron_workdir(token)
            self.assertIsInstance(resolve_agent_cwd(), Path)

    def test_attached_script_uses_workdir_and_closes_session_if_it_disappears(self):
        with TemporaryDirectory() as root:
            workdir = Path(root) / "workspace"
            workdir.mkdir()
            seen = []
            databases = []

            class FakeDB:
                def __init__(self):
                    self.closed = False
                    databases.append(self)

                def close(self):
                    self.closed = True

            def run_script(_job, _script, *, workdir, cancel_event):
                seen.append(workdir)
                Path(workdir).rmdir()
                return True, '{"wakeAgent": false}'

            job = {"id": "removed-during-script", "name": "Script", "prompt": "do work",
                   "schedule": "*/5 * * * *", "script": "check.py", "workdir": str(workdir)}
            with patch.dict(os.environ, {"HERMES_CRON_SESSION_DB_TIMEOUT": "0"}):
                with patch("hermes_state.SessionDB", FakeDB):
                    with patch.object(scheduler, "_run_job_script_with_claim_heartbeat", side_effect=run_script):
                        result = scheduler.run_job(job)

            self.assertEqual(seen, [str(workdir)])
            self.assertFalse(result[0])
            self.assertIn("workdir", result[3])
            self.assertEqual(len(databases), 1)
            self.assertTrue(databases[0].closed)
