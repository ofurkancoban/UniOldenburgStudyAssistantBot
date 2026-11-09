"""Per-student preferences engine (unibot.preferences) and where it acts:
exam and fee reminders, campus, diet, morning summary, wardrobe, previous
semester and the /settings menu."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import db, preferences, storage  # noqa: E402

WED = date(2026, 11, 4)


def _at(hh, mm=0, day=WED):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ_BERLIN)


class SchemaTests(unittest.TestCase):
    def test_defaults_are_complete_and_stored_per_student(self):
        p = preferences.get_prefs(8001)
        self.assertEqual(set(p), set(preferences.DEFAULTS))
        self.assertEqual((p["diet"], p["morning_time"], p["delivery_mode"]), ("all", "07:00", "instant"))
        preferences.set_pref(8001, "diet", "vegan")
        self.assertEqual(preferences.get_pref(8001, "diet"), "vegan")
        self.assertEqual(preferences.get_pref(8002, "diet"), "all")  # another student is untouched
        self.assertTrue(db.get_user_scope(8001).has_private(preferences.PREFS_KIND))  # encrypted, in the scope

    def test_invalid_values_are_rejected(self):
        bad = [("diet", "carnivore"), ("morning_time", "06:45"), ("morning_time", "07:10"),
               ("quiet_hours", {"start": "23:00"}), ("quiet_hours", {"start": "8:00", "end": "9:00"}),
               ("morning_sections", {"horoscope": True}), ("nope", 1)]
        for key, value in bad:
            with self.assertRaises(ValueError, msg=(key, value)):
                preferences.set_pref(8003, key, value)
        self.assertEqual(preferences.morning_time_options()[0], "07:00")
        self.assertEqual(preferences.morning_time_options()[-1], "09:30")
        self.assertEqual(len(preferences.morning_time_options()), 11)

    def test_old_global_settings_carry_over_to_the_primary_admin(self):
        db.save("food_preferences.json", {"avoid_codes": ["Kn"], "avoid_keywords": ["mushroom"]})
        self.addCleanup(db.delete, "food_preferences.json")
        with mock.patch.object(preferences, "ALLOWED_USER_IDS", {8010}), \
                mock.patch.object(preferences, "get_watch_previous_semester_enabled", lambda: True):
            admin = preferences.get_prefs(8010)
            other = preferences.get_prefs(8011)
        self.assertEqual((admin["diet"], admin["food_avoid_codes"], admin["watch_previous_semester"]),
                         ("halal", ["Kn"], True))
        self.assertEqual((other["diet"], other["food_avoid_codes"], other["watch_previous_semester"]),
                         ("all", [], False))

    def test_quiet_hours_across_midnight(self):
        preferences.set_pref(8004, "quiet_hours", {"start": "23:00", "end": "08:00"})
        self.assertTrue(preferences.in_quiet_hours(8004, _at(23, 30)))
        self.assertTrue(preferences.in_quiet_hours(8004, _at(7, 59)))
        self.assertFalse(preferences.in_quiet_hours(8004, _at(8, 0)))
        self.assertFalse(preferences.in_quiet_hours(8005, _at(3, 0)))  # none set
        preferences.set_pref(8004, "quiet_hours", {"start": "13:00", "end": "14:00"})
        self.assertTrue(preferences.in_quiet_hours(8004, _at(13, 30)))


class AcademicTests(unittest.TestCase):
    def test_exam_threshold_presets(self):
        self.assertEqual(preferences.exam_thresholds(None), ([14, 7, 3, 1], 3))
        preferences.set_pref(8020, "exam_thresholds", "critical")
        self.assertEqual(preferences.exam_thresholds(8020), ([3], None))

    def test_exam_reminders_follow_the_students_thresholds(self):
        import exam_reminder as er
        sent = []

        async def send(bot, text, **kw):
            sent.append(text)

        exams = [{"unit_id": "u1", "date": "2026-11-18", "title": "Statistics", "date_line": "18.11.2026",
                  "start_datetime": None}]
        preferences.set_pref(8021, "exam_thresholds", "standard")
        preferences.set_pref(8022, "exam_thresholds", "critical")
        with mock.patch.object(er, "get_registered_exam_schedule", mock.AsyncMock(return_value=exams)), \
                mock.patch.object(er, "berlin_now", lambda: _at(10)):
            for uid in (8021, 8022):
                with er.exam_cache_user(uid):
                    asyncio.run(er.check_exam_date_reminders(None, None, send))
        self.assertEqual(len(sent), 1)  # 14 days left: only the standard preset reminds
        self.assertIn("14 day(s) left", sent[0])

    def test_fee_reminder_modes(self):
        should = preferences.tuition_should_remind
        self.assertFalse(should("off", 2, 9, first_time=True))
        self.assertTrue(should("weekly", 30, None, first_time=True))
        self.assertFalse(should("weekly", 3, 2, first_time=False))
        self.assertTrue(should("weekly", 3, 7, first_time=False))
        self.assertFalse(should("daily_last7", 10, 2, first_time=False))
        self.assertTrue(should("daily_last7", 6, 1, first_time=False))
        self.assertFalse(should("daily_last7", 6, 0, first_time=False))  # once a day at most
        self.assertTrue(should("smart", 12, 1, first_time=False))
        self.assertFalse(should("smart", 30, 2, first_time=False))


class DailyLifeTests(unittest.TestCase):
    def test_campus_anchor_fills_in_where_classes_dont_say(self):
        from unibot import study_day
        day = WED
        window = {"start_dt": _at(9), "end_dt": _at(11), "duration_minutes": 120, "description": "free morning"}
        plain = study_day.plan_day([], [dict(window)], day)
        anchored = study_day.plan_day([], [dict(window)], day, anchor="wechloy")
        self.assertIsNone(plain["windows"][0]["campus"])
        self.assertEqual(anchored["windows"][0]["campus"], "wechloy")
        self.assertEqual(anchored["windows"][0]["study_spot"], study_day.CAMPUS_INFO["wechloy"]["study_spot"])
        self.assertEqual(anchored["lunch"]["menu_loc_id"], "3")
        preferences.set_pref(8030, "campus_anchor", "haarentor")
        self.assertEqual(preferences.campus_anchor(8030), "haarentor")
        self.assertIsNone(preferences.campus_anchor(8031))  # "both" follows the classes

    def test_diet_filters(self):
        veg = preferences.diet_filter("vegetarian")["avoid_codes"]
        self.assertTrue({"S", "G", "R", "Fi", "SG"} <= veg)
        self.assertTrue({"Ei", "Mi"} <= preferences.diet_filter("vegan")["avoid_codes"])
        fish = preferences.diet_filter("fish")["avoid_codes"]
        self.assertIn("G", fish)
        self.assertNotIn("Fi", fish)
        self.assertEqual(preferences.diet_filter("budget")["max_price"], 3.50)
        preferences.set_pref(8032, "diet", "halal")
        preferences.set_pref(8032, "food_avoid_keywords", ["Pilz"])
        rules = preferences.food_filter(8032)
        self.assertTrue({"S", "A"} <= rules["avoid_codes"])
        self.assertIn("pilz", rules["avoid_keywords"])

    def test_budget_diet_keeps_cheap_dishes_only(self):
        from unibot import mensa
        prompts = []

        async def llm(system, user, temperature=0.9):
            prompts.append(user)
            return '{"dish": "Pasta", "reasoning": "Cheap."}'

        items = [{"name": "Pasta", "category": "Mensa Wechloy", "description": "", "price": "2,90 €", "codes": set()},
                 {"name": "Steak", "category": "Mensa Wechloy", "description": "", "price": "5,90 €", "codes": {"R"}}]
        preferences.set_pref(8033, "diet", "budget")
        with mock.patch.object(mensa, "_speiseplan_main_items", mock.AsyncMock(return_value=items)), \
                mock.patch.object(mensa, "_call_openrouter", llm):
            pick = asyncio.run(mensa.build_food_recommendation("3", user_id=8033))
        self.assertEqual(pick["dish"], "Pasta")
        self.assertNotIn("Steak", prompts[0])

    def test_food_preferences_are_per_student(self):
        from unibot import mensa
        mensa.save_food_preferences(["Kn"], ["onion"], user_id=8034)
        self.assertEqual(mensa.load_food_preferences(8034), {"avoid_codes": ["Kn"], "avoid_keywords": ["onion"]})
        self.assertEqual(mensa.load_food_preferences(8035), {"avoid_codes": [], "avoid_keywords": []})

    def test_wardrobe_is_private_per_student_and_migrates(self):
        db.save(storage.WARDROBE_CACHE_PATH, {"8040": {"items": [{"id": "w_1", "label": "coat"}],
                                                        "reference_photo_file_id": "ref"}})
        self.assertEqual(storage.get_wardrobe_entry(8040)["items"][0]["label"], "coat")
        self.assertEqual(storage.get_wardrobe_entry(8041)["items"], [])
        self.assertEqual(db.load(storage.WARDROBE_CACHE_PATH), {})  # the shared document is emptied
        storage.add_wardrobe_item(8041, {"label": "scarf"}, file_id="f2")
        self.assertEqual([i["label"] for i in storage.get_wardrobe_entry(8040)["items"]], ["coat"])
        storage.save_wardrobe_item_descriptions({"w_1": "a warm wool coat"})
        self.assertEqual(storage.get_wardrobe_entry(8040)["items"][0]["description"], "a warm wool coat")
        self.assertTrue(db.get_user_scope(8040).has_private(storage.WARDROBE_KIND))


class MorningTests(unittest.TestCase):
    def setUp(self):
        from unibot import schedule, state
        self.schedule, self.state = schedule, state
        state._prepared_morning.clear()
        state._morning_delivery_tasks.clear()
        self.cache = {}
        for p in (mock.patch("unibot.morning.load_general_cache", lambda: dict(self.cache)),
                  mock.patch("unibot.morning.save_general_cache", lambda d: self.cache.update(d))):
            p.start()
            self.addCleanup(p.stop)

    def test_students_are_grouped_by_their_own_time(self):
        preferences.set_pref(8050, "morning_time", "07:00")
        preferences.set_pref(8051, "morning_time", "08:15")
        preferences.set_pref(8052, "morning_time", "off")
        prepared = []

        async def prepare(bot, uids):
            prepared.append(sorted(uids))
            return {"texts": {}}

        delivered = []

        async def deliver(bot, uids, data, send_at, today_str, slot):
            from unibot import morning
            delivered.append((slot, sorted(uids)))
            morning._mark_morning_summary_sent(today_str, slot)  # as the real delivery does

        with mock.patch("unibot.morning.prepare_morning_summary", prepare), \
                mock.patch("unibot.morning._deliver_morning_summary_at", deliver):
            async def run(now):
                await self.schedule.run_morning_summary_schedule(None, [8050, 8051, 8052], now)
                await asyncio.sleep(0)
            asyncio.run(run(_at(6, 40)))   # within 30 min of 07:00 only
            asyncio.run(run(_at(7, 50)))   # 08:15 group now
        self.assertEqual(prepared, [[8050], [8051]])
        self.assertEqual([slot for slot, _u in delivered], ["07:00", "08:15"])

    def test_already_sent_slots_are_not_repeated(self):
        from unibot import morning
        morning._mark_morning_summary_sent("2026-11-04", "07:00")
        self.assertTrue(morning.morning_summary_sent("2026-11-04", "07:00"))
        self.assertFalse(morning.morning_summary_sent("2026-11-04", "08:15"))
        self.cache.clear()
        self.cache["last_morning_summary"] = "2026-11-04"  # marked by the version before per-student times
        self.assertTrue(morning.morning_summary_sent("2026-11-04", "07:00"))
        self.assertFalse(morning.morning_summary_sent("2026-11-04", "07:15"))

    def test_hidden_sections_stay_out_of_the_summary(self):
        from unibot import morning
        preferences.set_pref(8053, "morning_sections", {"weather": False, "mensa": False, "exams": False,
                                                       "classes": True, "study": True})
        lunch = mock.AsyncMock(return_value=({"dish": "Soup"}, None, None))
        weather = {"summary": "rain"}
        with mock.patch.object(morning, "get_user_session", lambda uid=None: object()), \
                mock.patch.object(morning, "has_stored_login", lambda s: False), \
                mock.patch.object(morning, "user_tasks", lambda uid: []), \
                mock.patch.object(morning, "build_weather_text_line", lambda w: "WEATHER-LINE"), \
                mock.patch.object(morning, "_prepare_morning_voice", mock.AsyncMock(return_value=None)):
            text, _voice, _button, photo = asyncio.run(morning._prepare_user_summary(8053, WED, weather, lunch))
        self.assertNotIn("WEATHER-LINE", text)
        lunch.assert_not_called()
        self.assertEqual(photo, (None, None))


class PreviousSemesterTests(unittest.TestCase):
    def test_previous_semester_courses_only_for_opted_in_students(self):
        from unibot import monitoring, state
        monitoring._prev_semester_courses.clear()
        monitoring._course_list_fetched.clear()
        s1, s2 = mock.MagicMock(user_id=8060), mock.MagicMock(user_id=8061)
        preferences.set_pref(8060, "watch_previous_semester", True)
        saved = (dict(state.user_courses_map), state.global_session)
        self.addCleanup(lambda: (state.user_courses_map.clear(), state.user_courses_map.update(saved[0]),
                                 setattr(state, "global_session", saved[1])))
        state.global_session = None
        state.user_courses_map.update({8060: {"now1": "Math"}, 8061: {"now2": "Bio"}})
        accounts = [mock.MagicMock(user_id=8060, session=s1), mock.MagicMock(user_id=8061, session=s2)]
        fetched = []

        async def options(sess):
            return [("cur", "WiSe 26/27"), ("prev", "SoSe 26")]

        async def api(semester_id, session=None, user_id=None):
            fetched.append((semester_id, user_id))
            return [("Old Course", "old1")]

        with mock.patch.object(monitoring, "signed_in_accounts", lambda: accounts), \
                mock.patch.object(monitoring, "get_semester_options", options), \
                mock.patch.object(monitoring, "_list_courses_api", api), \
                mock.patch.object(monitoring, "load_default_semester", lambda: {}), \
                mock.patch.object(monitoring, "list_courses", mock.AsyncMock()), \
                mock.patch.object(monitoring.course_registry, "has_stored_login", lambda s: True):
            plan = asyncio.run(monitoring.watched_courses())
        self.assertEqual(fetched, [("prev", 8060)])  # with the student's own session, only for 8060
        self.assertEqual(sorted(cid for _n, cid, _s in plan), ["now1", "now2", "old1"])
        self.assertEqual(state.course_members["old1"], {8060})


class SettingsMenuTests(unittest.TestCase):
    def _tap(self, uid, data):
        from unibot import settings_menu
        query = mock.MagicMock()
        query.from_user.id = uid
        query.data = data
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        with mock.patch.object(settings_menu, "is_user_allowed", lambda u: True):
            asyncio.run(settings_menu.handle_settings(mock.MagicMock(callback_query=query), None))
        return query

    def test_buttons_change_preferences(self):
        uid = 8070
        q = self._tap(uid, "settings")
        self.assertIn("Settings", q.message.reply_text.call_args.args[0])
        self._tap(uid, "settings|cycle|diet")
        self.assertEqual(preferences.get_pref(uid, "diet"), "vegetarian")
        self._tap(uid, "settings|flip|outfit")
        self.assertFalse(preferences.get_pref(uid, "outfit"))
        self._tap(uid, "settings|mt|later")
        self._tap(uid, "settings|mt|later")
        self.assertEqual(preferences.get_pref(uid, "morning_time"), "07:30")
        self._tap(uid, "settings|mt|earlier")
        self.assertEqual(preferences.get_pref(uid, "morning_time"), "07:15")
        self._tap(uid, "settings|sec|weather")
        self.assertFalse(preferences.get_pref(uid, "morning_sections")["weather"])
        q = self._tap(uid, "settings|qh|23-08")
        self.assertEqual(preferences.get_pref(uid, "quiet_hours"), {"start": "23:00", "end": "08:00"})
        self.assertIn("23:00 to 08:00", q.edit_message_text.call_args.args[0])
        self._tap(uid, "settings|cycle|delivery_mode")
        self.assertEqual(preferences.get_pref(uid, "delivery_mode"), "digest")
        self._tap(uid, "settings|cycle|file_delivery")
        self.assertEqual(preferences.get_pref(uid, "file_delivery"), "upload")
        self._tap(uid, "settings|mt|off")
        self.assertIsNone(preferences.morning_send_time(uid))
        self._tap(uid, "settings|mt|on")
        self.assertEqual(preferences.morning_send_time(uid), time(7, 0))

    def test_every_page_renders(self):
        from unibot import settings_menu
        for page, view in settings_menu.PAGES.items():
            text, markup = view(8071)
            self.assertTrue(text and markup.inline_keyboard, page)
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len((button.callback_data or "").encode()), 64)


if __name__ == "__main__":
    unittest.main()
