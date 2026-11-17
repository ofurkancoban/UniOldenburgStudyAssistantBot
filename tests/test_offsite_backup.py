"""The nightly encrypted copy to Google Drive: the script (with stand-in rclone and age) and the
status the bot reads from it."""

import asyncio
import gzip
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import diagnostics, system  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "offsite_backup.sh")

FAKE_AGE = """#!/usr/bin/env bash
# stand-in for age: -R <recipient file> -o <out> <in>; "encrypts" by prefixing a marker
while [ $# -gt 0 ]; do case "$1" in -R) shift 2;; -o) out="$2"; shift 2;; *) in="$1"; shift;; esac; done
{ printf 'AGE-ENCRYPTED:'; cat "$in"; } > "$out"
"""

FAKE_RCLONE = """#!/usr/bin/env bash
# stand-in for rclone over a folder given in FAKE_DRIVE; every call is recorded in FAKE_CALLS
echo "$*" >> "$FAKE_CALLS"
cmd="$1"; shift
remote_path() { printf '%s/%s' "$FAKE_DRIVE" "${1#*:}"; }
case "$cmd" in
  mkdir) [ -n "$FAKE_RCLONE_FAILS" ] && exit 1; mkdir -p "$(remote_path "$1")";;
  copyto) cp "$1" "$(remote_path "$2")";;
  size) size=$(wc -c < "$(remote_path "$2")" | tr -d ' '); [ "$1" = "--json" ] && printf '{"count":1,"bytes":%s}' "$size";;
  lsf) ls -1 "$(remote_path "$1")";;
  delete) :;;
esac
"""


def _write_executable(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class OffsiteScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bot = os.path.join(self.tmp, "bot")
        self.bin = os.path.join(self.tmp, "bin")
        self.drive = os.path.join(self.tmp, "drive")
        for d in (self.bot, os.path.join(self.bot, "backups"), self.bin, self.drive):
            os.makedirs(d)
        with gzip.open(os.path.join(self.bot, "backups", "bot_data_2026-11-17.sqlite3.gz"), "wb") as fh:
            fh.write(b"database bytes")
        with open(os.path.join(self.bot, ".vault_key"), "w") as fh:
            fh.write("SECRET-KEY-CONTENT")
        with open(os.path.join(self.bot, ".offsite_age_recipient"), "w") as fh:
            fh.write("age1publickeyonly")
        _write_executable(os.path.join(self.bin, "age"), FAKE_AGE)
        _write_executable(os.path.join(self.bin, "rclone"), FAKE_RCLONE)
        self.calls = os.path.join(self.tmp, "calls.txt")

    def _run(self, **extra):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "BOT_DIR": self.bot,
               "FAKE_DRIVE": self.drive, "FAKE_CALLS": self.calls, "OFFSITE_REMOTE": "fake:UniAssistantBackup", **extra}
        return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=60)

    def _status(self):
        with open(os.path.join(self.bot, "backups", "offsite_status.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_good_run_uploads_the_database_and_the_key_encrypted(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        uploaded = sorted(os.listdir(os.path.join(self.drive, "UniAssistantBackup")))
        self.assertEqual(len(uploaded), 2)
        self.assertTrue(any(n == "bot_data_2026-11-17.sqlite3.gz.age" for n in uploaded), uploaded)
        self.assertTrue(any(n.startswith("vault_key_") and n.endswith(".age") for n in uploaded), uploaded)
        for name in uploaded:  # nothing readable left the server
            with open(os.path.join(self.drive, "UniAssistantBackup", name), "rb") as fh:
                content = fh.read()
            self.assertTrue(content.startswith(b"AGE-ENCRYPTED:"))
        status = self._status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["files"], 2)
        self.assertNotIn("SECRET-KEY-CONTENT", result.stdout + result.stderr)  # never printed

    def test_old_uploads_are_pruned_only_after_a_good_upload(self):
        self._run()
        with open(self.calls, encoding="utf-8") as fh:
            calls = fh.read().splitlines()
        self.assertTrue(any(c.startswith("delete ") and "--min-age 7d" in c for c in calls))
        self.assertLess([i for i, c in enumerate(calls) if c.startswith("copyto")][-1],
                        [i for i, c in enumerate(calls) if c.startswith("delete")][0])

    def test_a_failing_drive_is_reported_and_nothing_is_pruned(self):
        result = self._run(FAKE_RCLONE_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        status = self._status()
        self.assertFalse(status["ok"])
        self.assertIn("Drive folder", status["detail"])
        with open(self.calls, encoding="utf-8") as fh:
            self.assertFalse(any(c.startswith("delete") for c in fh.read().splitlines()))

    def test_without_a_public_key_or_a_backup_it_stops_with_a_reason(self):
        os.remove(os.path.join(self.bot, ".offsite_age_recipient"))
        self.assertNotEqual(self._run().returncode, 0)
        self.assertIn("public key", self._status()["detail"])
        with open(os.path.join(self.bot, ".offsite_age_recipient"), "w") as fh:
            fh.write("age1x")
        os.remove(os.path.join(self.bot, "backups", "bot_data_2026-11-17.sqlite3.gz"))
        self.assertNotEqual(self._run().returncode, 0)
        self.assertIn("no database backup", self._status()["detail"])


class OffsiteStatusTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = 1_800_000_000.0

    def _write(self, **status):
        with open(os.path.join(self.dir, "offsite_status.json"), "w", encoding="utf-8") as fh:
            json.dump(status, fh)

    def test_no_status_file_means_the_job_was_never_set_up(self):
        self.assertIsNone(diagnostics.offsite_backup_status(self.dir, self.now))
        self.assertIsNone(diagnostics.offsite_line(self.dir, self.now))

    def test_a_fresh_good_upload_is_healthy(self):
        self._write(ok=True, at=self.now - 3600, files=2)
        ok, detail = diagnostics.offsite_backup_status(self.dir, self.now)
        self.assertTrue(ok)
        self.assertIn("2 encrypted file", detail)
        self.assertIn("✅", diagnostics.offsite_line(self.dir, self.now))

    def test_a_failed_or_stale_upload_is_not_healthy(self):
        self._write(ok=False, at=self.now - 60, detail="could not reach the Drive folder")
        ok, detail = diagnostics.offsite_backup_status(self.dir, self.now)
        self.assertFalse(ok)
        self.assertIn("could not reach the Drive folder", detail)
        self._write(ok=True, at=self.now - 40 * 3600, files=2)
        ok, detail = diagnostics.offsite_backup_status(self.dir, self.now)
        self.assertFalse(ok)
        self.assertIn("40 hours ago", detail)
        self.assertIn("⚠️", diagnostics.offsite_line(self.dir, self.now))

    def test_a_broken_status_file_is_reported_not_raised(self):
        with open(os.path.join(self.dir, "offsite_status.json"), "w") as fh:
            fh.write("{not json")
        self.assertFalse(diagnostics.offsite_backup_status(self.dir, self.now)[0])

    def test_the_bots_health_check_includes_it_only_when_set_up(self):
        quiet = mock.AsyncMock(return_value=(True, "OK"))
        with mock.patch.object(system, "check_studip_health", quiet), \
                mock.patch.object(system, "check_whatsapp_health", quiet), \
                mock.patch.object(system, "check_weather_health", quiet), \
                mock.patch.object(system, "check_openrouter_health", quiet), \
                mock.patch.object(system, "check_gwdg_health", quiet):
            with mock.patch("unibot.diagnostics.offsite_backup_status", return_value=None):
                self.assertNotIn("offsite_backup", asyncio.run(system.run_health_checks()))
            with mock.patch("unibot.diagnostics.offsite_backup_status", return_value=(False, "stale")):
                results = asyncio.run(system.run_health_checks())
        self.assertEqual(results["offsite_backup"], (False, "stale"))
        self.assertIn("offsite_backup", system.HEALTH_CHECK_LABELS)

    def test_the_time_helper_is_used_for_age(self):
        self._write(ok=True, at=time.time() - 120, files=1)
        self.assertTrue(diagnostics.offsite_backup_status(self.dir)[0])


if __name__ == "__main__":
    unittest.main()
