"""Fixes from the isolation review: a scan shared with every follower skips
folders only some members see, voice notes need the student's OK before they
leave for an outside service, a student's calendar reminders live in their
encrypted record, and the study buddy screen says what the other student sees."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import studip_api, study_plan  # noqa: E402

UID = 9981


def _folder(name, folder_type):
    return {"type": "folder", "name": name, "url": f"https://x/{name}", "folder_type": folder_type}


def _file(name):
    return {"type": "file", "name": name, "url": f"https://x/f/{name}", "size": "1 KB",
            "modified": "-", "modified_iso": None}


class SharedScanFolderTests(unittest.TestCase):
    def _lister(self, tree):
        async def list_files(cid, folder_url=None, session=None, user_id=None):
            return tree[folder_url] if folder_url else tree[None]
        return list_files

    def test_restricted_folder_types_are_recognised(self):
        for kind in ("PermissionEnabledFolder", "HiddenFolder", "CourseGroupFolder", "HomeworkFolder", "TimedFolder"):
            self.assertFalse(studip_api.is_shared_folder(_folder("x", kind)), kind)
        for kind in ("StandardFolder", "MaterialFolder", "RootFolder", "CourseTopicFolder", None):
            self.assertTrue(studip_api.is_shared_folder(_folder("x", kind)), kind)

    def test_the_index_and_topics_scan_leaves_restricted_folders_out(self):
        tree = {None: [_file("root.pdf"), _folder("Slides", "StandardFolder"), _folder("Tutors only", "HiddenFolder"),
                       _folder("Group B", "CourseGroupFolder"), _folder("Unknown", None)],
                "https://x/Slides": [_file("week1.pdf")], "https://x/Tutors only": [_file("solutions.pdf")],
                "https://x/Group B": [_file("groupb.pdf")], "https://x/Unknown": [_file("html-only.pdf")]}
        with mock.patch.object(study_plan, "list_files", self._lister(tree)):
            files = asyncio.run(study_plan._list_course_files("c1"))
        self.assertEqual(sorted(f["name"] for _folder_name, f in files), ["html-only.pdf", "root.pdf", "week1.pdf"])

    def test_the_workload_scan_leaves_them_out_too(self):
        tree = {None: [_folder("20261013", "StandardFolder"), _folder("20261020", "HiddenFolder")],
                "https://x/20261013": [_file("slides.pdf"), _folder("PS", "HomeworkFolder"), _folder("Code", "StandardFolder")],
                "https://x/20261020": [_file("secret.pdf")], "https://x/PS": [_file("hand-ins.pdf")]}
        with mock.patch.object(study_plan, "list_files", self._lister(tree)):
            files = asyncio.run(study_plan._list_course_files_deep("c1"))
        names = sorted(f["name"] for _folder_name, f in files)
        self.assertEqual(names, ["slides.pdf"])

    def test_the_students_own_browsing_still_lists_everything(self):
        async def get_all(path, params=None, session=None, user_id=None):
            if path == "/courses/c1/folders":
                return [{"id": "root", "attributes": {"folder-type": "RootFolder"}, "relationships": {}}], []
            if path == "/folders/root/folders":
                return [{"id": "h", "attributes": {"name": "Tutors only", "folder-type": "HiddenFolder", "chdate": "2026-11-01T10:00:00+01:00"}}], []
            return [], []

        with mock.patch.object(studip_api, "studip_api_get_all", get_all):
            items = asyncio.run(studip_api._list_files_api("c1", None, session=object(), user_id=UID))
        self.assertEqual([(i["name"], i["folder_type"]) for i in items], [("Tutors only", "HiddenFolder")])  # shown, with its type


if __name__ == "__main__":
    unittest.main()
