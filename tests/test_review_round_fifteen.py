"""Fixes from the full-project review (batch 1): the watcher pause buttons use
the flag the watcher reads, long and markup-heavy messages are cut into valid
parts, the weather line is escaped for its message, and the privacy texts name
the services that really receive the data."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import formatting, menus, privacy, state, weather  # noqa: E402

UID = 9901


class WatcherPauseButtonTests(unittest.TestCase):
    def test_stop_and_start_flip_the_flag_the_watcher_reads(self):
        query = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID))
        with mock.patch.object(state, "global_watcher_paused", False):
            asyncio.run(menus._status_button_stop_watchers(update, None, query))
            self.assertTrue(state.global_watcher_paused)
            asyncio.run(menus._status_button_start_watchers(update, None, query))
            self.assertFalse(state.global_watcher_paused)


class EscapedChunkTests(unittest.TestCase):
    def test_every_part_fits_once_escaped(self):
        parts = formatting.escaped_chunks("&" * 3500 + "<b>" * 200, 3500)
        self.assertTrue(all(len(p) <= 3500 for p in parts))
        self.assertEqual("".join(parts).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"),
                         "&" * 3500 + "<b>" * 200)

    def test_no_entity_is_cut(self):
        for part in formatting.escaped_chunks("a&b" * 2000, 100):
            self.assertNotRegex(part, r"&[a-z]*$")


class SplitHtmlTests(unittest.TestCase):
    def test_open_tags_are_closed_and_reopened(self):
        text = "<b>" + "\n".join(f"line {i}" for i in range(400)) + "</b>"
        parts = formatting.split_html(text, 500)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 500)
            self.assertEqual(part.count("<b>"), part.count("</b>"))

    def test_a_very_long_line_is_cut_inside_its_text(self):
        parts = formatting.split_html("<i>" + "word &amp; " * 1000 + "</i>", 400)
        for part in parts:
            self.assertLessEqual(len(part), 400)
            self.assertEqual(part.count("<i>"), part.count("</i>"))
            self.assertNotRegex(part.replace("</i>", ""), r"&[a-z]*$")

    def test_short_text_is_one_part(self):
        self.assertEqual(formatting.split_html("<b>hi</b>\nthere"), ["<b>hi</b>\nthere"])


class WeatherLineTests(unittest.TestCase):
    SNAP = {"emoji": "x", "is_wet": False, "description": "light rain", "temp_min": 8, "temp_max": 12,
            "precip_chance": 10, "location_name": "Bad_Zwischenahn *north*"}

    def test_markdown_screen_gets_escaped_place(self):
        from studip_utils import md_escape
        line = weather.build_weather_text_line(self.SNAP, escape=md_escape)
        self.assertIn("Bad\\_Zwischenahn \\*north\\*", line)

    def test_html_message_gets_escaped_place(self):
        line = weather.build_weather_text_line({**self.SNAP, "location_name": "A & <B>"})
        self.assertIn("A &amp; &lt;B&gt;", line)


class ServiceNamesTests(unittest.TestCase):
    def test_privacy_screen_names_the_real_services(self):
        text, _markup = privacy.privacy_view(UID)
        for name in ("GWDG", "OpenRouter", "Google"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
