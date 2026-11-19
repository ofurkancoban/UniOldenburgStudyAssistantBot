"""Wardrobe upgrades: rotation by last worn date, liked outfits and "Another
one", a calendar line for the pick, fixing item types, importing from links,
and the photo reply for students who are not administrators."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import run_as, run_handler, use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import berlin_now  # noqa: E402
from unibot import storage, voice, wardrobe  # noqa: E402

UID = 7401


def _tags(label, type_="top", subtype="t-shirt", color="black", warmth="light"):
    return {"type": type_, "subtype": subtype, "color": color, "warmth": warmth, "label": label}


def _fresh_wardrobe(uid=UID):
    storage.clear_wardrobe_items(uid)
    return {
        "tee": storage.add_wardrobe_item(uid, _tags("black tee"), image_url="https://x/tee.jpg"),
        "tee2": storage.add_wardrobe_item(uid, _tags("white tee", color="white"), image_url="https://x/tee2.jpg"),
        "jeans": storage.add_wardrobe_item(uid, _tags("blue jeans", "bottom", "pants", "blue", "medium"), image_url="https://x/j.jpg"),
    }


def _query(data, uid=UID):
    query = mock.MagicMock()
    query.data = data
    query.from_user = types.SimpleNamespace(id=uid)
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    query.edit_message_reply_markup = mock.AsyncMock()
    query.message = mock.AsyncMock()
    return types.SimpleNamespace(callback_query=query), query


class StorageTests(unittest.TestCase):
    def test_days_since_worn_uses_the_latest_pick(self):
        items = _fresh_wardrobe()
        old = (berlin_now().date() - timedelta(days=5)).isoformat()
        storage.save_personal_record(UID, "outfit_history", {old: [items["tee"]["id"]]})
        storage.record_worn_items(UID, [items["tee"]["id"], items["jeans"]["id"]])
        days = storage.get_days_since_worn(UID)
        self.assertEqual(days[items["tee"]["id"]], 0)
        self.assertEqual(days[items["jeans"]["id"]], 0)
        self.assertNotIn(items["tee2"]["id"], days)

    def test_likes_count_and_forget_removed_items(self):
        items = _fresh_wardrobe()
        storage.add_outfit_likes(UID, [items["tee"]["id"], "w_gone"])
        storage.add_outfit_likes(UID, [items["tee"]["id"]])
        self.assertEqual(storage.get_outfit_likes(UID), {items["tee"]["id"]: 2})
        storage.remove_wardrobe_item(UID, items["tee"]["id"])
        storage.add_outfit_likes(UID, [items["jeans"]["id"]])
        self.assertEqual(storage.get_outfit_likes(UID), {items["jeans"]["id"]: 1})

    def test_update_item_changes_only_allowed_fields(self):
        items = _fresh_wardrobe()
        updated = storage.update_wardrobe_item(UID, items["tee"]["id"], type="outerwear", subtype="coat", label="hacked")
        self.assertEqual((updated["type"], updated["subtype"], updated["label"]), ("outerwear", "coat", "black tee"))
        self.assertIsNone(storage.update_wardrobe_item(UID, "w_missing", type="top"))

    def test_cached_outfit_keeps_item_ids(self):
        storage.save_cached_outfit_today(UID, "d", None, "", ["a"], ["w_1"])
        self.assertEqual(storage.get_cached_outfit_today(UID)["item_ids"], ["w_1"])


class PickTests(unittest.TestCase):
    def setUp(self):
        self.items = _fresh_wardrobe()
        self.entry_items = storage.get_wardrobe_entry(UID)["items"]
        self.weather = {"temp_max": 20, "temp_min": 12, "is_wet": False, "description": "sunny"}

    def test_rotation_prefers_the_item_worn_longest_ago(self):
        storage.save_personal_record(UID, "outfit_history", {
            (berlin_now().date() - timedelta(days=9)).isoformat(): [self.items["tee"]["id"]],
            (berlin_now().date() - timedelta(days=3)).isoformat(): [self.items["tee2"]["id"]],
        })
        prompts = []

        async def fake_llm(system, user, **kwargs):
            prompts.append(user)
            return None  # fall back to the heuristic

        with mock.patch.object(wardrobe, "_call_openrouter", fake_llm), \
                mock.patch.object(wardrobe, "personal_ai_allowed", return_value=True):
            outfit = run_as(UID, wardrobe.pick_outfit_from_wardrobe(self.entry_items, self.weather, set(), user_id=UID))
        labels = [it["label"] for it in outfit["chosen_items"]]
        self.assertIn("black tee", labels)  # worn 9 days ago beats worn 3 days ago
        self.assertNotIn("white tee", labels)
        self.assertTrue(all("_rank" not in it for it in outfit["chosen_items"]))
        self.assertIn("[worn 9 days ago]", prompts[0])
        self.assertIn("[not worn recently]", prompts[0])

    def test_schedule_line_and_likes_reach_the_prompt(self):
        storage.add_outfit_likes(UID, [self.items["jeans"]["id"]])
        prompts = []

        async def fake_llm(system, user, **kwargs):
            prompts.append(user)
            return None

        with mock.patch.object(wardrobe, "_call_openrouter", fake_llm), \
                mock.patch.object(wardrobe, "personal_ai_allowed", return_value=True):
            run_as(UID, wardrobe.pick_outfit_from_wardrobe(
                storage.get_wardrobe_entry(UID)["items"], self.weather, set(), user_id=UID,
                day_context="Schedule: 2 class(es) between 08:00 and 16:00."))
        self.assertIn("Schedule: 2 class(es) between 08:00 and 16:00.", prompts[0])
        self.assertIn("[liked before]", prompts[0])

    def test_prefer_unworn_keeps_the_old_behaviour_without_ranks(self):
        pool = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual([i["id"] for i in wardrobe._prefer_unworn(pool, {"a"})], ["b", "c", "a"])
        self.assertIs(wardrobe._prefer_unworn(pool, set()), pool)


class DayContextTests(unittest.TestCase):
    def test_line_has_times_and_campuses_but_no_course_names(self):
        events = [
            {"title": "Secret Seminar", "time": "08:15 - 09:45", "location": "A14 1-101", "date_key": 1},
            {"title": "Other Lecture", "time": "14:00 - 15:30", "location": "W02 1-A01", "date_key": 1},
            {"title": "Cancelled", "time": "11:00 - 12:00", "location": "A1", "date_key": 1, "is_cancelled": True},
        ]
        session = mock.MagicMock()
        session.is_logged_in = mock.AsyncMock(return_value=True)
        fetch = mock.AsyncMock(return_value=(events, events, True))
        with mock.patch.object(wardrobe, "get_user_session", return_value=session), \
                mock.patch("unibot.morning._fetch_today_events", fetch):
            line = asyncio.run(wardrobe.outfit_day_context(UID, berlin_now().date()))
        self.assertEqual(line, "Schedule: 2 class(es) between 08:15 and 15:30, on both campuses.")
        self.assertNotIn("Secret", line)

    def test_no_classes_and_unreadable_calendar(self):
        session = mock.MagicMock()
        session.is_logged_in = mock.AsyncMock(return_value=True)
        with mock.patch.object(wardrobe, "get_user_session", return_value=session):
            with mock.patch("unibot.morning._fetch_today_events", mock.AsyncMock(return_value=([], [], True))):
                self.assertEqual(asyncio.run(wardrobe.outfit_day_context(UID, berlin_now().date())),
                                 "Schedule: no classes today.")
            with mock.patch("unibot.morning._fetch_today_events", mock.AsyncMock(return_value=([], [], False))):
                self.assertIsNone(asyncio.run(wardrobe.outfit_day_context(UID, berlin_now().date())))


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.items = _fresh_wardrobe()
        ids = [self.items["tee"]["id"], self.items["jeans"]["id"]]
        storage.save_cached_outfit_today(UID, "d", None, "", ["black tee", "blue jeans"], ids)
        self.ids = ids

    def test_like_counts_the_pieces_and_removes_the_buttons(self):
        update, query = _query("outfit_fb|like")
        with mock.patch.object(wardrobe, "is_user_admin", return_value=True):
            run_handler(wardrobe.handle_outfit_feedback, update, mock.MagicMock())
        self.assertEqual(storage.get_outfit_likes(UID), {i: 1 for i in self.ids})
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    def test_another_asks_again_without_todays_pieces(self):
        update, query = _query("outfit_fb|another")
        run = mock.AsyncMock()
        ready = mock.AsyncMock(return_value=(["i"], "ref"))
        with mock.patch.object(wardrobe, "is_user_admin", return_value=True), \
                mock.patch.object(wardrobe, "_outfit_ready", ready), \
                mock.patch.object(wardrobe, "_run_outfit_request", run):
            run_handler(wardrobe.handle_outfit_feedback, update, mock.MagicMock())
        self.assertEqual(run.await_args.kwargs["exclude_ids"], set(self.ids))
        self.assertEqual(storage.get_outfit_likes(UID), {})

    def test_an_outfit_without_stored_pieces_is_too_old(self):
        storage.save_cached_outfit_today(UID, "d", None)
        update, query = _query("outfit_fb|like")
        with mock.patch.object(wardrobe, "is_user_admin", return_value=True):
            run_handler(wardrobe.handle_outfit_feedback, update, mock.MagicMock())
        query.answer.assert_awaited_once_with("That outfit is too old, ask for a new one.", show_alert=True)

    def test_students_are_refused(self):
        update, query = _query("outfit_fb|like")
        with mock.patch.object(wardrobe, "is_user_admin", return_value=False):
            run_handler(wardrobe.handle_outfit_feedback, update, mock.MagicMock())
        query.answer.assert_awaited_once_with("⛔ This feature is restricted to administrators.", show_alert=True)


class EditTests(unittest.TestCase):
    def setUp(self):
        self.items = _fresh_wardrobe()

    def _press(self, data, admin=True):
        update, query = _query(data)
        with mock.patch.object(wardrobe, "is_user_admin", return_value=admin):
            run_handler(wardrobe.handle_wardrobe_edit, update, mock.MagicMock())
        return query

    def test_list_view_offers_the_fix_button(self):
        keyboard = wardrobe._wardrobe_list_view(UID)[1]
        self.assertIn("wardrobe_edit_list", [b.callback_data for row in keyboard.inline_keyboard for b in row])

    def test_changing_the_type_keeps_a_subtype_only_if_it_still_fits(self):
        tee = self.items["tee"]["id"]
        self._press(f"wardrobe_set|{tee}|type|outerwear")
        item = next(i for i in storage.get_wardrobe_entry(UID)["items"] if i["id"] == tee)
        self.assertEqual((item["type"], item["subtype"]), ("outerwear", None))
        self._press(f"wardrobe_set|{tee}|subtype|coat")
        self._press(f"wardrobe_set|{tee}|warmth|warm")
        item = next(i for i in storage.get_wardrobe_entry(UID)["items"] if i["id"] == tee)
        self.assertEqual((item["subtype"], item["warmth"]), ("coat", "warm"))

    def test_impossible_values_are_ignored(self):
        tee = self.items["tee"]["id"]
        for data in (f"wardrobe_set|{tee}|type|spaceship", f"wardrobe_set|{tee}|subtype|coat",
                     f"wardrobe_set|{tee}|warmth|lava", f"wardrobe_set|{tee}|label|x"):
            self._press(data)
        item = next(i for i in storage.get_wardrobe_entry(UID)["items"] if i["id"] == tee)
        self.assertEqual((item["type"], item["subtype"], item["warmth"], item["label"]),
                         ("top", "t-shirt", "light", "black tee"))

    def test_students_cannot_edit(self):
        tee = self.items["tee"]["id"]
        query = self._press(f"wardrobe_set|{tee}|type|shoes", admin=False)
        query.answer.assert_awaited_once_with("⛔ This feature is restricted to administrators.", show_alert=True)
        item = next(i for i in storage.get_wardrobe_entry(UID)["items"] if i["id"] == tee)
        self.assertEqual(item["type"], "top")


class ImportTests(unittest.TestCase):
    def _public(self, url):
        return asyncio.run(wardrobe._is_public_https_url(url))

    def test_only_public_https_links_pass(self):
        def fake_infos(address):
            return mock.AsyncMock(return_value=[(2, 1, 6, "", (address, 443))])

        for url in ("http://example.com/a.jpg", "https://user:pw@example.com/a.jpg", "https:///a.jpg", "https://example.com:bad/a.jpg"):
            self.assertFalse(self._public(url), url)
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1"):
            loop = asyncio.new_event_loop()
            try:
                with mock.patch.object(loop, "getaddrinfo", fake_infos(address)):
                    asyncio.set_event_loop(loop)
                    self.assertFalse(loop.run_until_complete(wardrobe._is_public_https_url("https://shop.example/a.jpg")), address)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        loop = asyncio.new_event_loop()
        try:
            with mock.patch.object(loop, "getaddrinfo", fake_infos("93.184.216.34")):
                self.assertTrue(loop.run_until_complete(wardrobe._is_public_https_url("https://shop.example/a.jpg")))
        finally:
            loop.close()

    def test_import_adds_skips_and_reports(self):
        storage.clear_wardrobe_items(UID)
        storage.add_wardrobe_item(UID, _tags("existing"), image_url="https://x/old.jpg")
        images = {"https://x/new.jpg": b"img", "https://x/bad-tag.jpg": b"img2"}

        async def fetch(url):
            return images.get(url)

        async def tag(data, user_id):
            return _tags("new hoodie", "top", "sweater") if data == b"img" else None

        with mock.patch.object(wardrobe, "_fetch_public_image", fetch), \
                mock.patch.object(wardrobe, "tag_wardrobe_item", tag), \
                mock.patch.object(wardrobe, "describe_wardrobe_item", mock.AsyncMock(return_value="a grey hoodie")), \
                mock.patch.object(wardrobe, "personal_ai_allowed", return_value=True):
            result = run_as(UID, wardrobe.import_wardrobe_urls(UID, [
                "https://x/old.jpg", "https://x/new.jpg", "https://x/bad-tag.jpg", "https://x/gone.jpg"]))
        self.assertEqual(result["added"], ["new hoodie"])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual([why for _, why in result["failed"]],
                         ["could not tell what it is", "could not download an image"])
        added = next(i for i in storage.get_wardrobe_entry(UID)["items"] if i["label"] == "new hoodie")
        self.assertEqual((added["image_url"], added["description"]), ("https://x/new.jpg", "a grey hoodie"))

    def test_import_stops_when_personal_ai_is_switched_off(self):
        storage.clear_wardrobe_items(UID)
        with mock.patch.object(wardrobe, "personal_ai_allowed", return_value=False):
            result = run_as(UID, wardrobe.import_wardrobe_urls(UID, ["https://x/a.jpg"]))
        self.assertTrue(result["stopped"])
        self.assertEqual(storage.get_wardrobe_entry(UID)["items"], [])

    def test_backfill_fills_only_missing_descriptions(self):
        storage.clear_wardrobe_items(UID)
        done = storage.add_wardrobe_item(UID, dict(_tags("done"), description="has one"), image_url="https://x/1.jpg")
        todo = storage.add_wardrobe_item(UID, _tags("todo"), image_url="https://x/2.jpg")
        with mock.patch.object(wardrobe, "_fetch_wardrobe_item_bytes", mock.AsyncMock(return_value=b"img")), \
                mock.patch.object(wardrobe, "describe_wardrobe_item", mock.AsyncMock(return_value="new text")):
            count = run_as(UID, wardrobe.backfill_wardrobe_descriptions(UID))
        self.assertEqual(count, 1)
        by_id = {i["id"]: i for i in storage.get_wardrobe_entry(UID)["items"]}
        self.assertEqual((by_id[done["id"]]["description"], by_id[todo["id"]]["description"]), ("has one", "new text"))

    def test_command_is_for_administrators_only(self):
        message = mock.MagicMock()
        message.text = "/wardrobe_import https://x/a.jpg"
        message.reply_text = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID), message=message)
        with mock.patch.object(wardrobe, "is_user_admin", return_value=False):
            run_handler(wardrobe.wardrobe_import_command, update, mock.MagicMock())
        message.reply_text.assert_awaited_once_with("⛔ This command is restricted to administrators.")


class PhotoReplyTests(unittest.TestCase):
    def _send(self, admin):
        message = mock.MagicMock()
        message.photo = [types.SimpleNamespace(file_id="f1")]
        message.reply_text = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID), message=message)
        context = types.SimpleNamespace(user_data={})
        with mock.patch.object(voice, "is_user_allowed", return_value=True), \
                mock.patch.object(voice, "is_user_admin", return_value=admin):
            run_handler(voice.handle_photo_message, update, context)
        return message.reply_text.await_args.args[0]

    def test_only_the_administrator_is_pointed_to_the_wardrobe(self):
        self.assertIn("My Wardrobe", self._send(admin=True))
        self.assertNotIn("Wardrobe", self._send(admin=False))


if __name__ == "__main__":
    unittest.main()
