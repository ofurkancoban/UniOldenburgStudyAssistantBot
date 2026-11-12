"""Exercise the deployment shell with local stand-ins, never production services."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class DeploymentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="unibot-release-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "cloudflare_worker").mkdir()
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / "remote/.venv/bin").mkdir(parents=True)
        (self.root / "bin").mkdir()
        shutil.copy(Path(__file__).resolve().parents[1] / "scripts/deploy.sh", self.root / "scripts/deploy.sh")
        (self.root / "head").write_text("oldcommit")
        self.version_ids = ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
        self.env = {**os.environ, "DEPLOY_HOST": "fake-host", "DEPLOY_DIR": str(self.root / "remote"),
                    "PM2_APP": "UniAssistant", "NODE_BIN": str(self.root / "bin"),
                    "HEALTH_TIMEOUT": "1", "ROLLBACK_TIMEOUT": "1", "FAKE_ROOT": str(self.root),
                    "PYTHON": str(self.root / ".venv/bin/python"), "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}"}
        self.stub(".venv/bin/ruff", "pass")
        self.stub(".venv/bin/mypy", "pass")
        self.stub("bin/npm", "sys.exit(1 if os.environ.get('FAIL_NPM') == '1' else 0)")
        self.stub(".venv/bin/python", """
            if sys.argv[1:3] == ['-m', 'unittest']: sys.exit(0)
            os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
        """)
        self.stub("bin/git", """
            root = Path(os.environ['FAKE_ROOT'])
            remote = Path.cwd().resolve() == (root / 'remote').resolve()
            args = sys.argv[1:]
            if args[0] == 'rev-parse': print((root / 'head').read_text() if remote else 'abcdef0')
            elif args[0] == 'pull': (root / 'head').write_text('abcdef0')
            elif args[0] == 'reset': (root / 'head').write_text(args[-1])
            elif remote and args[0] == 'diff' and 'requirements.txt' in args and os.environ.get('FAIL_FREEZE') == '1': sys.exit(1)
        """)
        self.stub("bin/npx", """
            root = Path(os.environ['FAKE_ROOT'])
            args = sys.argv[1:]
            with (root / 'worker_calls').open('a') as log: log.write(json.dumps(args) + '\\n')
            if args[2:4] == ['deployments', 'status']:
                print(json.dumps({'versions': [
                    {'version_id': '00000000-0000-0000-0000-000000000001', 'percentage': 60},
                    {'version_id': '00000000-0000-0000-0000-000000000002', 'percentage': 40}]}))
            elif args[2:] == ['deploy'] and os.environ.get('FAIL_WORKER') == '1': sys.exit(1)
        """)
        self.stub("bin/ssh", """
            code = sys.stdin.read()
            if os.environ.get('FAIL_SSH') == '1': sys.exit(1)
            env = dict(os.environ)
            for arg in sys.argv[2:]:
                if '=' in arg:
                    key, value = arg.split('=', 1)
                    env[key] = value
            result = subprocess.run(['bash', '-s'], input=code, text=True, env=env)
            sys.exit(result.returncode)
        """)
        self.stub("bin/pm2", """
            root = Path(os.environ['FAKE_ROOT'])
            output, error = root / 'out.log', root / 'err.log'
            if sys.argv[1] == 'jlist':
                if os.environ.get('FAIL_AFTER_UPDATE') == '1' and (root / 'head').read_text() == 'abcdef0': sys.exit(1)
                print(json.dumps([{'name': 'UniAssistant', 'pid': 10101, 'pm2_env': {
                    'status': 'online', 'pm_out_log_path': str(output), 'pm_err_log_path': str(error)}}]))
            elif sys.argv[1] == 'restart':
                old = (root / 'head').read_text() == 'oldcommit'
                marker = ('Watcher cycle #1 completed' if old else
                          'Watcher cycle healthy pid=10101\\nDeadline reminder cycle healthy pid=10101' if os.environ.get('HEALTH_MODE') == 'ready' else
                          'Watcher cycle healthy pid=10101' if os.environ.get('HEALTH_MODE') == 'no-deadlines' else
                          'Watcher cycle healthy pid=99999' if os.environ.get('HEALTH_MODE') == 'wrong-pid' else
                          'Unified watcher controller started successfully')
                with output.open('a') as log:
                    log.write('Telegram polling started successfully\\n' + marker + '\\n')
        """)
        self.stub("bin/sleep", "import time; time.sleep(0.05)")
        self.stub("bin/timeout", "os.execvp(sys.argv[2], sys.argv[2:])")
        self.stub("remote/.venv/bin/pip", "sys.exit(1)")
        (self.root / "out.log").write_text("Telegram polling started successfully\nWatcher cycle healthy pid=10101\n")
        (self.root / "err.log").write_text("")

    def stub(self, name, code):
        path = self.root / name
        path.write_text(f"#!{Path(sys.executable).resolve()}\nimport os, sys, json, subprocess\nfrom pathlib import Path\n" + textwrap.dedent(code))
        path.chmod(0o700)

    def run_release(self, **settings):
        return subprocess.run(["bash", str(self.root / "scripts/deploy.sh"), "--worker"],
                              env={**self.env, **settings}, capture_output=True, text=True, timeout=20)

    def worker_calls(self):
        return [json.loads(line) for line in (self.root / "worker_calls").read_text().splitlines()]

    def test_worker_dependency_failure_stops_before_external_deploy(self):
        result = self.run_release(FAIL_NPM="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "worker_calls").exists())
        self.assertEqual((self.root / "head").read_text(), "oldcommit")

    def assert_worker_restored(self):
        call = self.worker_calls()[-1]
        self.assertEqual(call[2:4], ["versions", "deploy"])
        self.assertIn(f"{self.version_ids[0]}@60", call)
        self.assertIn(f"{self.version_ids[1]}@40", call)

    def test_healthy_new_cycle_keeps_both_releases(self):
        result = self.run_release(HEALTH_MODE="ready")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "head").read_text(), "abcdef0")
        self.assertFalse(any(call[2:4] == ["versions", "deploy"] for call in self.worker_calls()))

    def test_startup_and_old_log_markers_are_not_healthy(self):
        result = self.run_release(HEALTH_MODE="startup-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "head").read_text(), "oldcommit")
        self.assertIn("rolled back to oldcommit and healthy", result.stderr)
        self.assert_worker_restored()

    def test_another_process_marker_is_not_healthy(self):
        result = self.run_release(HEALTH_MODE="wrong-pid")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "head").read_text(), "oldcommit")
        self.assert_worker_restored()

    def test_controller_without_deadline_loop_is_not_healthy(self):
        result = self.run_release(HEALTH_MODE="no-deadlines")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "head").read_text(), "oldcommit")
        self.assert_worker_restored()

    def test_ssh_failure_restores_worker_traffic_split(self):
        result = self.run_release(FAIL_SSH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_worker_restored()

    def test_uncertain_worker_deploy_failure_restores_previous_versions(self):
        result = self.run_release(FAIL_WORKER="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_worker_restored()

    def test_unexpected_failure_after_pull_restores_both_releases(self):
        result = self.run_release(FAIL_FREEZE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "head").read_text(), "oldcommit")
        self.assertIn("Interrupted release; restoring oldcommit", result.stderr)
        self.assert_worker_restored()
