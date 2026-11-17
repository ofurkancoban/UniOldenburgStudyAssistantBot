"""Lunch recommendations are personal: liked ingredients weigh most, dishes given
in the last days weigh down, nothing is shared between students."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, run_as, run_handler, use_temp_db  # noqa: E402
use_temp_db()

from unibot import mensa, menus, preferences  # noqa: E402

A, B, C = 7731, 7732, 7733
TODAY = date(2026, 11, 17)


def _dish(name, description="", price="3,20 €", codes=()):
    return {"name": name, "category": "Mensa Uhlhornsweg", "description": description, "price": price,
            "codes": set(codes)}


MENU = [
    _dish("Hähnchenbrust mit Reis", "Geflügel, Reis", codes={"G"}),
    _dish("Spaghetti Bolognese", "Rinderhack, Nudeln", codes={"R"}),
    _dish("Gemüsecurry mit Reis", "Linsen, Gemüse", codes={"V"}),
    _dish("Tofu Bowl", "Tofu, Brokkoli", codes={"V+"}),
    _dish("Käsespätzle", "Käse, Spätzle", codes={"V"}),
    _dish("Lachsfilet", "Lachs, Kartoffeln", codes={"F"}),
]


class LikeMatchingTests(unittest.TestCase):
    def test_words_are_split_lowered_and_deduplicated(self):
        self.assertEqual(mensa.parse_food_words("Chicken, Pasta and tofu\nchicken; ; x"), ["chicken", "pasta", "tofu"])
        self.assertEqual(mensa.parse_food_words(""), [])

    def test_an_english_word_finds_the_german_dish(self):
        chicken = mensa.matched_likes(MENU[0], ["chicken"])
        self.assertEqual(chicken, ["chicken"])
        self.assertEqual(mensa.matched_likes(MENU[1], ["pasta"]), ["pasta"])  # Nudeln
        self.assertEqual(mensa.matched_likes(MENU[4], ["cheese"]), ["cheese"])  # Käse
        self.assertEqual(mensa.matched_likes(MENU[1], ["chicken"]), [])

    def test_a_short_word_only_counts_as_a_whole_word(self):
        eintopf = _dish("Linseneintopf", "Eintopf")
        self.assertEqual(mensa.matched_likes(eintopf, ["egg"]), [])
        self.assertEqual(mensa.matched_likes(_dish("Rührei", "mit Ei"), ["egg"]), ["egg"])


class RankingTests(unittest.TestCase):
    def test_liked_ingredients_come_first(self):
        ranked = mensa.rank_candidates(MENU, ["tofu"], [], A, TODAY)
        self.assertEqual(ranked[0][1]["name"], "Tofu Bowl")
        self.assertEqual(ranked[0][2], ["tofu"])

    def test_more_liked_ingredients_rank_higher(self):
        ranked = mensa.rank_candidates(MENU, ["rice", "chicken"], [], A, TODAY)
        self.assertEqual(ranked[0][1]["name"], "Hähnchenbrust mit Reis")

    def test_a_dish_given_today_or_lately_drops(self):
        recent = [{"date": TODAY.isoformat(), "dish": "Tofu Bowl"}]
        ranked = mensa.rank_candidates(MENU, ["tofu"], recent, A, TODAY)
        self.assertNotEqual(ranked[0][1]["name"], "Tofu Bowl")
        old = [{"date": (TODAY - timedelta(days=12)).isoformat(), "dish": "Tofu Bowl"}]
        self.assertEqual(mensa.rank_candidates(MENU, ["tofu"], old, A, TODAY)[0][1]["name"], "Tofu Bowl")

    def test_students_with_different_tastes_get_different_dishes(self):
        first_a = mensa.rank_candidates(MENU, ["tofu"], [], A, TODAY)[0][1]["name"]
        first_b = mensa.rank_candidates(MENU, ["lachs"], [], B, TODAY)[0][1]["name"]
        self.assertNotEqual(first_a, first_b)

    def test_without_any_taste_students_and_days_do_not_all_get_the_same_dish(self):
        firsts = {mensa.rank_candidates(MENU, [], [], uid, TODAY + timedelta(days=day))[0][1]["name"]
                  for uid in range(9000, 9012) for day in range(5)}
        self.assertGreaterEqual(len(firsts), 3, firsts)

    def test_the_ranking_is_stable_for_one_student_and_day(self):
        one = [r[1]["name"] for r in mensa.rank_candidates(MENU, [], [], A, TODAY)]
        two = [r[1]["name"] for r in mensa.rank_candidates(MENU, [], [], A, TODAY)]
        self.assertEqual(one, two)


class PreferenceWordsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for uid in (A, B, C):
            activate_student(uid)

    def test_words_are_saved_per_student_and_move_between_lists(self):
        added, ignored = mensa.add_food_words(A, "like", "chicken, mushrooms")
        self.assertEqual((added, ignored), (["chicken", "mushrooms"], []))
        self.assertEqual(mensa.load_food_preferences(B)["like_keywords"], [])  # nobody else
        added, ignored = mensa.add_food_words(A, "avoid", "mushrooms, fish")
        prefs = mensa.load_food_preferences(A)
        self.assertEqual(prefs["like_keywords"], ["chicken"])  # moved to the avoided list
        self.assertEqual(prefs["avoid_keywords"], ["mushrooms", "fish"])
        added, ignored = mensa.add_food_words(A, "like", "chicken")
        self.assertEqual((added, ignored), ([], ["chicken"]))

    def test_likes_reach_the_food_filter(self):
        mensa.add_food_words(C, "like", "Tofu")
        self.assertEqual(preferences.food_filter(C)["like_keywords"], ["tofu"])
        self.assertEqual(preferences.food_filter(None)["like_keywords"], [])

    def test_invalid_stored_values_are_rejected(self):
        with self.assertRaises(ValueError):
            preferences.validate("food_like_keywords", "tofu")
        self.assertEqual(preferences.validate("food_like_keywords", [" tofu ", ""]), ["tofu"])


class RecommendationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for uid in (A, B):
            activate_student(uid)

    def _recommend(self, uid, llm=None, menu=None):
        llm = llm or mock.AsyncMock(return_value=None)  # the model is unreachable: the ranking decides
        with mock.patch.object(mensa, "_speiseplan_main_items", mock.AsyncMock(return_value=menu or MENU)), \
                mock.patch.object(mensa, "_call_openrouter", llm), \
                mock.patch.object(mensa, "berlin_now", lambda: types.SimpleNamespace(date=lambda: TODAY)):
            return run_as(uid, mensa.build_food_recommendation("3", user_id=uid))

    def test_the_pick_follows_the_students_likes_and_is_not_random(self):
        tofu_fan, fish_fan = 7740, 7741
        for uid in (tofu_fan, fish_fan):
            activate_student(uid)
        mensa.add_food_words(tofu_fan, "like", "tofu")
        mensa.add_food_words(fish_fan, "like", "lachs")
        self.assertEqual(self._recommend(tofu_fan)["dish"], "Tofu Bowl")
        self.assertEqual(self._recommend(fish_fan)["dish"], "Lachsfilet")

    def test_asking_again_gives_something_else_and_the_history_is_the_students_own(self):
        uid = 7734
        activate_student(uid)
        mensa.add_food_words(uid, "like", "reis")
        first = self._recommend(uid)["dish"]
        second = self._recommend(uid)["dish"]
        self.assertNotEqual(first, second)
        history = run_as(uid, _async(lambda: mensa._recent_picks(uid, TODAY)))
        self.assertEqual([h["dish"] for h in history], [first, second])
        self.assertEqual(run_as(B, _async(lambda: [h for h in mensa._recent_picks(B, TODAY) if h["dish"] in (first, second)])), [])

    def test_the_model_only_sees_the_shortlist_with_the_students_tastes_and_history(self):
        uid = 7735
        activate_student(uid)
        mensa.add_food_words(uid, "like", "tofu")
        seen = []

        async def llm(system, prompt, **kwargs):
            seen.append(prompt)
            return '{"dish": "Tofu Bowl", "reasoning": "You like tofu."}'

        pick = self._recommend(uid, llm=llm)
        self.assertEqual(pick["dish"], "Tofu Bowl")
        self.assertIn("[likes: tofu]", seen[0])
        self.assertIn("The student likes: tofu", seen[0])
        self.assertLessEqual(seen[0].count("\n- "), mensa.FOOD_SHORTLIST)
        self.assertNotIn("Recently recommended", seen[0])  # a first request has no history
        self._recommend(uid, llm=llm)
        self.assertIn("Recently recommended: Tofu Bowl", seen[1])

    def test_a_dish_with_an_avoided_ingredient_is_never_picked_even_if_liked_elsewhere(self):
        uid = 7736
        activate_student(uid)
        mensa.add_food_words(uid, "like", "reis, tofu")
        mensa.add_food_words(uid, "avoid", "tofu")
        for _ in range(4):
            self.assertNotEqual(self._recommend(uid)["dish"], "Tofu Bowl")


class SpokenFoodPreferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for uid in (7780, 7781, 7782, 7783):
            activate_student(uid)

    def _voice(self, uid, intent, query):
        from unibot import voice
        message = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid), effective_message=message)
        run_as(uid, voice._voice_intent_food_words(update, None, {}, "", intent, query, [], message))
        return message.reply_text.call_args.args[0]

    def test_every_food_intent_is_known_labelled_and_routed(self):
        from unibot import voice
        for intent in ("food_like", "food_avoid", "food_allow"):
            self.assertIn(intent, voice.VOICE_INTENTS)
            self.assertIn(intent, voice.INTENT_LABELS)
            self.assertIn(intent, voice._VOICE_INTENT_HANDLERS)
            self.assertIn(intent + ":", voice.CLASSIFY_INTENT_SYSTEM_PROMPT)

    def test_i_like_saves_liked_ingredients(self):
        reply = self._voice(7780, "food_like", "chicken, pasta")
        self.assertIn("chicken, pasta", reply)
        self.assertEqual(mensa.load_food_preferences(7780)["like_keywords"], ["chicken", "pasta"])

    def test_i_dont_eat_hides_the_word_and_its_official_codes(self):
        self._voice(7781, "food_avoid", "pork, mushrooms")
        prefs = mensa.load_food_preferences(7781)
        self.assertEqual(prefs["avoid_keywords"], ["pork", "mushrooms"])
        self.assertEqual(set(prefs["avoid_codes"]), {"S", "Sch", "Su"})
        self.assertEqual(mensa.load_food_preferences(7780)["avoid_codes"], [])  # nobody else

    def test_i_eat_it_again_takes_the_word_and_its_codes_off(self):
        self._voice(7782, "food_avoid", "pork, fish")
        reply = self._voice(7782, "food_allow", "pork")
        self.assertIn("pork", reply)
        prefs = mensa.load_food_preferences(7782)
        self.assertEqual(prefs["avoid_keywords"], ["fish"])
        self.assertEqual(set(prefs["avoid_codes"]), {"F", "Fi"})  # fish stays avoided

    def test_a_code_stays_while_another_avoided_word_still_needs_it(self):
        self._voice(7783, "food_avoid", "pork, schwein")
        self._voice(7783, "food_allow", "schwein")
        self.assertEqual(set(mensa.load_food_preferences(7783)["avoid_codes"]), {"S", "Sch", "Su"})

    def test_nothing_to_allow_and_nothing_understood_are_said_plainly(self):
        self.assertIn("None of those", self._voice(7780, "food_allow", "caviar"))
        self.assertIn("didn't catch", self._voice(7780, "food_like", ""))


class MenuCacheTests(unittest.TestCase):
    def test_the_public_menu_is_fetched_once_for_all_students_within_minutes(self):
        calls = []

        async def fetch(loc, date_str):
            calls.append((loc, date_str))
            return "<html>menu</html>"

        mensa._SPEISEPLAN_CACHE.clear()

        async def scenario():
            return [await mensa._fetch_speiseplan_html("3") for _ in range(3)]

        with mock.patch.object(mensa, "_fetch_speiseplan_html_uncached", fetch):
            pages = asyncio.run(scenario())
        self.assertEqual(pages, ["<html>menu</html>"] * 3)
        self.assertEqual(len(calls), 1)

    def test_a_failed_fetch_is_not_remembered(self):
        calls = []

        async def fetch(loc, date_str):
            calls.append(loc)
            return None

        mensa._SPEISEPLAN_CACHE.clear()

        async def scenario():
            await mensa._fetch_speiseplan_html("3")
            await mensa._fetch_speiseplan_html("3")

        with mock.patch.object(mensa, "_fetch_speiseplan_html_uncached", fetch):
            asyncio.run(scenario())
        self.assertEqual(len(calls), 2)


class MorningPickIsPersonalTests(unittest.TestCase):
    def test_the_summary_no_longer_shares_one_pick_between_students(self):
        with open(os.path.join(ROOT, "unibot", "morning.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("key = (loc_id, uid)", source)
        self.assertNotIn('food_rules["diet"], tuple(sorted(food_rules["avoid_codes"]))', source)


class PreferenceScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_student(7737)

    def _view(self, uid):
        async def build():
            return mensa._food_preferences_management_view("main")
        return run_as(uid, build())

    def test_the_screen_lists_likes_and_offers_the_new_buttons(self):
        mensa.add_food_words(7737, "like", "pasta")
        text, markup = self._view(7737)
        self.assertIn("pasta", text)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("foodpref_add|like", data)
        self.assertIn("foodpref_add|avoid", data)
        self.assertTrue(any(d.startswith("foodpref_unlike|k0:") for d in data))

    def test_the_add_button_waits_for_words_and_the_next_message_saves_them(self):
        uid = 7737
        query = mock.AsyncMock()
        query.data = "foodpref_add|like"
        query.from_user = types.SimpleNamespace(id=uid)
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=uid))
        context = types.SimpleNamespace(user_data={}, bot=mock.AsyncMock())
        with mock.patch.object(mensa, "is_user_allowed", return_value=True):
            run_handler(mensa.handle_foodpref_add, update, context)
        self.assertEqual(context.user_data["pending_step"], "food_like_add")

        message = mock.AsyncMock()
        message.text = "curry, lentils"
        update = types.SimpleNamespace(callback_query=None, message=message, effective_message=message,
                                       effective_user=types.SimpleNamespace(id=uid))
        self.assertTrue(run_handler(menus.handle_pending_step, update, context))
        self.assertNotIn("pending_step", context.user_data)
        self.assertEqual(mensa.load_food_preferences(uid)["like_keywords"][-2:], ["curry", "lentils"])

    def test_a_stale_unlike_button_removes_nothing(self):
        uid = 7737
        before = mensa.load_food_preferences(uid)["like_keywords"]
        query = mock.AsyncMock()
        query.data = "foodpref_unlike|k0:deadbeef"
        query.from_user = types.SimpleNamespace(id=uid)
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=uid))
        with mock.patch.object(mensa, "is_user_allowed", return_value=True):
            run_handler(mensa.handle_foodpref_unlike, update, types.SimpleNamespace(user_data={}))
        self.assertEqual(mensa.load_food_preferences(uid)["like_keywords"], before)


def _async(function):
    async def runner():
        return function()
    return runner()


if __name__ == "__main__":
    unittest.main()
