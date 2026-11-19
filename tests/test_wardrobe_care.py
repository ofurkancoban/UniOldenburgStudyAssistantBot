"""Wardrobe care: laundry state, what is missing, outfit history, the evening
preview, the midday rain alert, image provider statistics and their alert."""

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
from unibot import preferences, storage, watcher, weather, wardrobe  # noqa: E402

UID = 7402


def _tags(label, type_="top", subtype="t-shirt", color="black", warmth="light"):
    return {"type": type_, "subtype": subtype, "color": color, "warmth": warmth, "label": label}


def _day(offset):
    return (berlin_now().date() + timedelta(days=offset)).isoformat()


def _fresh(uid=UID):
    storage.clear_wardrobe_items(uid)
    storage.save_personal_record(uid, "outfit_history", {})
    storage.save_personal_record(uid, "outfit_likes", {})
    return {
        "tee": storage.add_wardrobe_item(uid, _tags("black tee"), image_url="https://x/1"),
        "tee2": storage.add_wardrobe_item(uid, _tags("white tee", color="white"), image_url="https://x/2"),
        "jeans": storage.add_wardrobe_item(uid, _tags("blue jeans", "bottom", "pants", "blue", "medium"), image_url="https://x/3"),
        "coat": storage.add_wardrobe_item(uid, _tags("grey coat", "outerwear", "coat", "grey", "warm"), image_url="https://x/4"),
    }


def _query(data, uid=UID):
    query = mock.MagicMock()
    query.data = data
    query.from_user = types.SimpleNamespace(id=uid)
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    query.message = mock.AsyncMock()
    return types.SimpleNamespace(callback_query=query), query


class LaundryStateTests(unittest.TestCase):
    def setUp(self):
        self.items = _fresh()

    def test_only_tops_and_bottoms_worn_on_an_earlier_day_are_dirty(self):
        storage.save_personal_record(UID, "outfit_history", {
            _day(-1): [self.items["tee"]["id"], self.items["coat"]["id"]],
            _day(0): [self.items["jeans"]["id"]],
        })
        self.assertEqual(storage.dirty_item_ids(UID), {self.items["tee"]["id"]})

    def test_the_pick_of_today_is_dirty_tomorrow(self):
        storage.save_personal_record(UID, "outfit_history", {_day(0): [self.items["jeans"]["id"]]})
        self.assertEqual(storage.dirty_item_ids(UID, berlin_now().date() + timedelta(days=1)), {self.items["jeans"]["id"]})

    def test_washing_cleans_until_it_is_worn_again(self):
        tee = self.items["tee"]["id"]
        storage.save_personal_record(UID, "outfit_history", {_day(-2): [tee]})
        self.assertEqual(storage.dirty_item_ids(UID), {tee})
        self.assertEqual(storage.mark_items_clean(UID), 1)
        self.assertEqual(storage.dirty_item_ids(UID), set())
        # worn again after that wash: dirty from the next day on
        storage.update_wardrobe_item(UID, tee, clean_since=_day(-1))
        storage.save_personal_record(UID, "outfit_history", {_day(0): [tee]})
        tomorrow = berlin_now().date() + timedelta(days=1)
        self.assertEqual(storage.dirty_item_ids(UID, tomorrow), {tee})
        self.assertEqual(storage.dirty_item_ids(UID), set())  # today's pick is not dirty yet

    def test_apply_laundry_keeps_a_type_alive_and_warns_when_low(self):
        tops = [{"id": "a", "type": "top"}, {"id": "b", "type": "top"}, {"id": "c", "type": "top"}]
        bottoms = [{"id": "d", "type": "bottom"}]
        clean, note = wardrobe._apply_laundry(tops + bottoms, {"a", "b"})
        self.assertEqual([i["id"] for i in clean], ["c", "d"])
        self.assertIn("1 clean top left", note)
        clean, note = wardrobe._apply_laundry(tops + bottoms, {"a", "b", "c", "d"})
        self.assertEqual(len(clean), 4)
        self.assertIn("no clean tops left", note)
        self.assertIn("no clean bottoms left", note)
        self.assertEqual(wardrobe._apply_laundry(tops + bottoms, set())[1], "")

    def test_the_pick_leaves_the_dirty_top_out_and_says_so(self):
        storage.save_personal_record(UID, "outfit_history", {_day(-1): [self.items["tee"]["id"]]})

        async def no_llm(*args, **kwargs):
            return None

        weather_now = {"temp_max": 15, "temp_min": 11, "is_wet": False, "description": "cloudy"}
        with mock.patch.object(wardrobe, "_call_openrouter", no_llm), \
                mock.patch.object(wardrobe, "personal_ai_allowed", return_value=True):
            outfit = run_as(UID, wardrobe.pick_outfit_from_wardrobe(
                storage.get_wardrobe_entry(UID)["items"], weather_now, set(), user_id=UID))
        labels = [i["label"] for i in outfit["chosen_items"]]
        self.assertIn("white tee", labels)
        self.assertNotIn("black tee", labels)
        self.assertIn("1 clean top left", outfit["laundry_note"])
        self.assertIn("🧺 Laundry", wardrobe.outfit_caption(outfit))
        self.assertNotIn("Laundry", wardrobe.outfit_caption({**outfit, "laundry_note": ""}))


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.items = _fresh()
        storage.save_personal_record(UID, "outfit_history", {_day(-1): [self.items["tee"]["id"], self.items["jeans"]["id"]]})

    def _press(self, data, admin=True):
        update, query = _query(data)
        with mock.patch.object(wardrobe, "is_user_admin", return_value=admin):
            run_handler(wardrobe.handle_wardrobe_edit, update, mock.MagicMock())
        return query

    def _shown(self, query):
        args, kwargs = query.edit_message_text.await_args
        buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        return args[0], buttons

    def test_laundry_screen_washes_one_or_all(self):
        text, buttons = self._shown(self._press("wardrobe_laundry"))
        self.assertIn("2 piece(s)", text)
        self.assertIn(f"wardrobe_wash|{self.items['tee']['id']}", buttons)
        text, buttons = self._shown(self._press(f"wardrobe_wash|{self.items['tee']['id']}"))
        self.assertIn("1 piece(s)", text)
        text, buttons = self._shown(self._press("wardrobe_wash_all"))
        self.assertIn("Nothing is waiting", text)
        self.assertEqual(storage.dirty_item_ids(UID), set())

    def test_gaps_screen_lists_what_is_missing(self):
        text, _ = self._shown(self._press("wardrobe_gaps"))
        self.assertIn("No shorts", text)
        self.assertIn("No sweater", text)
        self.assertIn("Only 0 pair(s) of shoes", text)

    def test_history_screen_shows_labels_and_likes(self):
        storage.add_outfit_likes(UID, [self.items["tee"]["id"]])
        text, _ = self._shown(self._press("wardrobe_history"))
        self.assertIn("black tee, blue jeans", text)
        self.assertIn("Most liked: black tee (1)", text)

    def test_students_cannot_open_them(self):
        query = self._press("wardrobe_wash_all", admin=False)
        query.answer.assert_awaited_once_with("⛔ This feature is restricted to administrators.", show_alert=True)
        self.assertEqual(len(storage.dirty_item_ids(UID)), 2)

    def test_list_view_has_the_new_buttons(self):
        buttons = [b.callback_data for row in wardrobe._wardrobe_list_view(UID)[1].inline_keyboard for b in row]
        for expected in ("wardrobe_laundry", "wardrobe_gaps", "wardrobe_history", "wardrobe_edit_list"):
            self.assertIn(expected, buttons)


class GapAndDuplicateTests(unittest.TestCase):
    def test_similar_items_need_the_same_kind_color_and_words(self):
        a = _tags("black and purple Umbro track jacket", "outerwear", "light_jacket")
        b = _tags("black Umbro track jacket", "outerwear", "light_jacket")
        c = _tags("black Vans t-shirt")
        d = _tags("white Vans t-shirt", color="white")
        self.assertTrue(wardrobe._looks_alike(a, b))
        self.assertFalse(wardrobe._looks_alike(c, d))
        self.assertEqual(len(wardrobe.find_similar_items([a, b, c, d])), 1)

    def test_a_complete_wardrobe_has_no_gaps(self):
        items = [_tags(f"top{i}") for i in range(4)] + [_tags("sweater", subtype="sweater", warmth="warm")]
        items += [_tags(f"b{i}", "bottom", "pants", "blue", "medium") for i in range(2)] + [_tags("shorts", "bottom", "shorts")]
        items += [_tags("coat", "outerwear", "coat"), _tags("jacket", "outerwear", "light_jacket")]
        items += [_tags("sneakers", "shoes", None), _tags("boots", "shoes", None), _tags("black beanie", "accessory", None)]
        self.assertEqual(wardrobe.wardrobe_gaps(items), [])


class ScheduledExtrasTests(unittest.TestCase):
    def setUp(self):
        self.items = _fresh()
        preferences.set_pref(UID, "outfit_evening", True)
        preferences.set_pref(UID, "outfit_midday", True)
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()

    def _preview(self, **overrides):
        snapshot = {"temp_max": 14, "temp_min": 8, "is_wet": True, "description": "light rain", "emoji": "🌧"}
        patches = {
            "is_user_admin": mock.Mock(return_value=True),
            "personal_ai_allowed": mock.Mock(return_value=True),
            "in_quiet_hours": mock.Mock(return_value=False),
            "get_weather_snapshot": mock.AsyncMock(return_value=snapshot),
            "outfit_day_context": mock.AsyncMock(return_value="Schedule: no classes today."),
            "_call_openrouter": mock.AsyncMock(return_value=None),
        }
        patches.update(overrides)
        with mock.patch.multiple(wardrobe, **patches):
            return run_as(UID, wardrobe.send_evening_outfit_preview(self.bot, UID))

    def test_evening_preview_is_text_and_not_recorded_as_worn(self):
        self.assertTrue(self._preview())
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Tomorrow's outfit (preview)", text)
        self.assertIn("rain expected", text)
        self.assertEqual(storage.get_outfit_history(UID), {})

    def test_evening_preview_respects_the_switch_quiet_hours_and_missing_forecast(self):
        preferences.set_pref(UID, "outfit_evening", False)
        self.assertFalse(self._preview())
        preferences.set_pref(UID, "outfit_evening", True)
        self.assertFalse(self._preview(in_quiet_hours=mock.Mock(return_value=True)))
        self.assertFalse(self._preview(get_weather_snapshot=mock.AsyncMock(return_value=None)))
        self.assertFalse(self._preview(is_user_admin=mock.Mock(return_value=False)))
        self.bot.send_message.assert_not_awaited()

    def _midday(self, outlook, baseline_wet=False, with_outerwear=False):
        storage.save_cached_outfit_today(UID, "d", None, "", ["x"], [self.items["coat"]["id"]] if with_outerwear else ["w_none"])
        storage.save_outfit_weather_baseline(UID, {"is_wet": baseline_wet})
        with mock.patch.multiple(wardrobe, is_user_admin=mock.Mock(return_value=True),
                                 in_quiet_hours=mock.Mock(return_value=False),
                                 get_afternoon_outlook=mock.AsyncMock(return_value=outlook)):
            return run_as(UID, wardrobe.send_midday_weather_alert(self.bot, UID))

    def test_rain_alert_goes_out_once_with_advice_that_fits_the_outfit(self):
        self.assertTrue(self._midday({"is_wet": True, "precip_chance": 80}))
        self.assertIn("Take a jacket or an umbrella", self.bot.send_message.await_args.kwargs["text"])
        with mock.patch.multiple(wardrobe, is_user_admin=mock.Mock(return_value=True),
                                 in_quiet_hours=mock.Mock(return_value=False),
                                 get_afternoon_outlook=mock.AsyncMock(return_value={"is_wet": True})):
            self.assertFalse(run_as(UID, wardrobe.send_midday_weather_alert(self.bot, UID)))  # already alerted
        self.assertEqual(self.bot.send_message.await_count, 1)

    def test_rain_alert_mentions_the_jacket_when_the_outfit_has_one(self):
        self.assertTrue(self._midday({"is_wet": True}, with_outerwear=True))
        self.assertIn("Your jacket should cover it", self.bot.send_message.await_args.kwargs["text"])

    def test_no_alert_when_it_was_already_wet_stays_dry_or_nothing_was_sent_today(self):
        self.assertFalse(self._midday({"is_wet": True}, baseline_wet=True))
        self.assertFalse(self._midday({"is_wet": False}))
        self.assertFalse(self._midday(None))
        storage.save_personal_record(UID, "outfit_weather_baseline", {"date": _day(-1), "is_wet": False, "alerted": False})
        with mock.patch.multiple(wardrobe, is_user_admin=mock.Mock(return_value=True),
                                 in_quiet_hours=mock.Mock(return_value=False),
                                 get_afternoon_outlook=mock.AsyncMock(return_value={"is_wet": True})):
            self.assertFalse(run_as(UID, wardrobe.send_midday_weather_alert(self.bot, UID)))
        self.bot.send_message.assert_not_awaited()

    def test_afternoon_outlook_reads_the_hours_after_noon(self):
        day = _day(0)
        hourly = {
            "time": [f"{day}T09:00", f"{day}T13:00", f"{day}T15:00"],
            "weathercode": [0, 3, 61],
            "precipitation_probability": [90, 10, 20],
        }
        self.assertEqual(weather.afternoon_outlook_from_hourly(hourly, day), {"is_wet": True, "precip_chance": 20})
        hourly["weathercode"] = [0, 3, 3]
        self.assertFalse(weather.afternoon_outlook_from_hourly(hourly, day)["is_wet"])  # the 90% was at 09:00
        hourly["precipitation_probability"] = [0, 70, 0]
        self.assertTrue(weather.afternoon_outlook_from_hourly(hourly, day)["is_wet"])
        self.assertIsNone(weather.afternoon_outlook_from_hourly(hourly, _day(1)))

    def test_outfit_menu_has_switches_for_both(self):
        update, query = _query("outfit_menu_evening")
        with mock.patch.object(wardrobe, "is_user_admin", return_value=True):
            run_handler(wardrobe.handle_outfit_menu, update, mock.MagicMock())
        self.assertFalse(preferences.get_pref(UID, "outfit_evening"))
        update, query = _query("outfit_menu_midday")
        with mock.patch.object(wardrobe, "is_user_admin", return_value=True):
            run_handler(wardrobe.handle_outfit_menu, update, mock.MagicMock())
        self.assertFalse(preferences.get_pref(UID, "outfit_midday"))
        self.assertIn("Evening preview (20:00): 🔲 Off", query.edit_message_text.await_args.args[0])

    def test_preferences_accept_only_booleans_for_the_new_switches(self):
        for key in ("outfit_evening", "outfit_midday"):
            self.assertIs(preferences.validate(key, True), True)
            with self.assertRaises(ValueError):
                preferences.validate(key, "yes")

    def test_the_watcher_runs_them_only_in_their_hours(self):
        calls = []

        async def fake_once(key, period, label, start, job, *args):
            calls.append(key)

        with mock.patch.object(watcher, "_run_once_per_period", fake_once):
            for hour in (10, 13, 20, 23):
                now = berlin_now().replace(hour=hour)
                asyncio.run(watcher._run_outfit_midday(self.bot, now))
                asyncio.run(watcher._run_outfit_evening(self.bot, now))
        self.assertEqual(calls, ["last_outfit_midday", "last_outfit_evening"])


class ImageStatsTests(unittest.TestCase):
    def setUp(self):
        storage.update_general_cache({"outfit_stats": {}})
        wardrobe._last_image_alert = 0.0
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()

    def _generate(self, provider):
        async def chain(bot, ref, items, user_id, used):
            if provider:
                used.append(provider)
                return b"img"
            return None

        with mock.patch.object(wardrobe, "_tryon_chain", chain), \
                mock.patch.object(wardrobe, "personal_ai_allowed", return_value=True), \
                mock.patch.object(wardrobe, "ADMIN_USER_IDS", {111}):
            return asyncio.run(wardrobe.generate_outfit_tryon_image(self.bot, b"ref", [], user_id=UID))

    def test_the_winning_provider_is_counted_and_costs_show_in_the_digest(self):
        self._generate("gemini")
        self._generate("gemini")
        self._generate("gwdg")
        stats = storage.get_outfit_stats()[_day(0)]
        self.assertEqual(stats, {"gemini": 2, "gwdg": 1})
        line = wardrobe.outfit_digest_line()
        self.assertIn("2 Nano Banana", line)
        self.assertIn("1 GWDG", line)
        self.assertIn("$0.16", line)
        self.bot.send_message.assert_not_awaited()

    def test_total_failure_counts_and_alerts_once_per_cooldown(self):
        self.assertIsNone(self._generate(None))
        self.assertIsNone(self._generate(None))
        self.assertEqual(storage.get_outfit_stats()[_day(0)], {"none": 2})
        self.bot.send_message.assert_awaited_once()
        args = self.bot.send_message.await_args.kwargs
        self.assertEqual(args["chat_id"], 111)
        self.assertIn("every provider failed", args["text"])
        self.assertIn("2 failed", wardrobe.outfit_digest_line())

    def test_a_stopped_request_is_not_a_failure(self):
        async def chain(bot, ref, items, user_id, used):
            return None

        with mock.patch.object(wardrobe, "_tryon_chain", chain), \
                mock.patch.object(wardrobe, "personal_ai_allowed", return_value=False):
            asyncio.run(wardrobe.generate_outfit_tryon_image(self.bot, b"ref", [], user_id=UID))
        self.assertEqual(storage.get_outfit_stats(), {})
        self.bot.send_message.assert_not_awaited()

    def test_digest_line_is_empty_until_something_was_counted(self):
        self.assertIsNone(wardrobe.outfit_digest_line())

    def test_stats_are_pruned_and_hold_no_user_data(self):
        old = (berlin_now().date() - timedelta(days=60)).isoformat()
        storage.update_general_cache({"outfit_stats": {old: {"gemini": 5}}})
        storage.record_outfit_stat("gemini")
        stats = storage.get_outfit_stats()
        self.assertNotIn(old, stats)
        self.assertNotIn(str(UID), str(stats))


if __name__ == "__main__":
    unittest.main()
