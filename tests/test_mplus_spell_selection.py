"""Offline coverage for the timeline's per-spell monitoring selection."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server


class SpellSelectionApiTest(unittest.TestCase):
    def setUp(self):
        self.start = 100000
        self.meta = {
            "fights": [{"id": 2, "start_time": self.start, "end_time": self.start + 60000,
                        "dungeonPulls": [{"id": 1, "start_time": self.start + 10000,
                                          "end_time": self.start + 20000}]}],
            "friendlies": [{"id": 1, "name": "战士", "type": "Warrior", "fights": ".2.",
                            "icon": "Warrior-Protection"}],
            "enemies": [],
        }
        self.graph = {"series": [self.series(118038, "剑在人在", [5]),
                                 self.series(999001, "自选技能", [4, 16, 50]),
                                 self.series(999002, "未选技能", [17])]}
        self.buffs = [self.buff(999003, "自选技能"), self.buff(999004, "未选技能"),
                      self.buff(999005, "剑在人在"), self.buff(6940, "牺牲祝福")]

    def series(self, spell, name, times):
        return {"guid": spell, "name": name, "events": [
            {"type": "cast", "sourceID": 1, "timestamp": self.start + t * 1000,
             "ability": {"abilityIcon": "test_icon.jpg"}} for t in times]}

    def buff(self, spell, name):
        return {"type": "applybuff", "sourceID": 1, "targetID": 1,
                "timestamp": self.start + 8000, "ability": {"guid": spell, "name": name}}

    def run_api(self, extra=()):
        with (patch.object(server, "report_meta", return_value=self.meta),
              patch.object(server, "graph", side_effect=[self.graph, {}, {}]),
              patch.object(server, "source_graph", return_value={}),
              patch.object(server, "mplus_buff_events", return_value=self.buffs) as fetch):
            return server.api_mplus("report", 2, extra), fetch.call_args

    def test_default_catalog_includes_full_run_casts_and_ignores_unselected_auras(self):
        # This native buff belongs to the player's class but has no cast in this run.
        self.buffs[-1] = self.buff(871, "盾墙")
        data, fetch = self.run_api()
        options = {s["spell"]: s for s in data["spellOptions"]}
        self.assertEqual(set(options), {118038, 999001, 999002, 871})
        self.assertEqual(options[999001]["count"], 3)  # includes cast at 50s, outside the pull
        self.assertEqual(options[999001]["cat"], "custom")
        self.assertEqual(options[871]["count"], 0)
        self.assertEqual(data["trackedSpells"], [])
        self.assertEqual([b["spell"] for b in data["buffs"]], [871, 999005])
        self.assertEqual(next(b for b in data["buffs"] if b["spell"] == 999005)["trackSpell"], 118038)
        self.assertEqual([c["spell"] for c in data["pulls"][0]["casts"]], [118038])
        self.assertEqual([c["t"] for c in data["pulls"][0]["extraCasts"]], [4, 16, 17])
        self.assertEqual([u["spell"] for u in data["players"][0]["usage"]], [118038])
        self.assertEqual(data["lust"], [])
        self.assertNotIn(999001, fetch.args[4])
        self.assertNotIn("自选技能", fetch.args[5])
        self.assertNotIn("未选技能", fetch.args[5])

    def test_selected_custom_cast_tracks_same_name_aura_without_changing_usage(self):
        data, fetch = self.run_api([999001, 999006])
        self.assertEqual(data["trackedSpells"], [999001, 999006])
        self.assertTrue({999001, 999006}.issubset(fetch.args[4]))
        self.assertIn("自选技能", fetch.args[5])
        self.assertNotIn("未选技能", fetch.args[5])
        buff = next(b for b in data["buffs"] if b["spell"] == 999003)
        self.assertEqual((buff["cat"], buff["trackSpell"]), ("custom", 999001))
        options = {s["spell"]: s for s in data["spellOptions"]}
        self.assertIn(999001, options)
        self.assertNotIn(999003, options)
        self.assertEqual(options[999001]["count"], 3)
        self.assertEqual(len(data["players"][0]["usage"]), 1)
        self.assertEqual(len(data["pulls"][0]["casts"]), 1)

    def test_directly_selected_buff_only_id_is_exposed(self):
        self.buffs = [self.buff(999006, "无施法增益")]
        data, fetch = self.run_api([999006])
        self.assertEqual(data["buffs"][0]["cat"], "custom")
        self.assertNotIn("trackSpell", data["buffs"][0])
        option = next(s for s in data["spellOptions"] if s["spell"] == 999006)
        self.assertEqual((option["cat"], option["count"]), ("custom", 0))


class SpellSelectionIntervalsTest(unittest.TestCase):
    def intervals(self, casts, extra, spell=999003, owner=1):
        players = {1: {"type": "Warrior"}, 2: {"type": "Warrior"}}
        events = [{"type": kind, "timestamp": t, "sourceID": owner, "targetID": 1,
                   "ability": {"guid": spell, "name": "同名技能"}}
                  for kind, t in [("applybuff", 1000), ("removebuff", 5000)]]
        return server.mplus_buff_intervals(events, casts, players, 0, 10000, extra_spells=extra)

    def test_selected_native_spell_keeps_native_category(self):
        buff, = self.intervals([], [118038], spell=118038)
        self.assertEqual(buff["cat"], "defensive")

    def test_native_cast_name_category_takes_precedence_in_either_order(self):
        casts = [{"who": 1, "spell": 118038, "name": "同名技能", "cat": "defensive"},
                 {"who": 1, "spell": 999001, "name": "同名技能", "cat": "custom"}]
        for ordered in (casts, list(reversed(casts))):
            with self.subTest(order=[cast["spell"] for cast in ordered]):
                buff, = self.intervals(ordered, [999001])
                self.assertEqual(buff["cat"], "defensive")
                self.assertEqual(buff["trackSpell"], 118038)

    def test_direct_alias_id_selection_keeps_original_aura_and_canonical_cast_ids(self):
        casts = [{"who": 1, "spell": 999001, "name": "同名技能", "cat": "custom"}]
        buff, = self.intervals(casts, [999003])
        self.assertEqual((buff["spell"], buff["trackSpell"], buff["cat"]),
                         (999003, 999001, "custom"))

    def test_unselected_custom_cast_does_not_expand_aura_matching(self):
        casts = [{"who": 1, "spell": 999001, "name": "同名技能", "cat": "custom"}]
        self.assertEqual(self.intervals(casts, []), [])
        buff, = self.intervals(casts, [999001])
        self.assertEqual(buff["trackSpell"], 999001)

    def test_same_name_alias_requires_unique_id_for_the_owner(self):
        casts = [{"who": who, "spell": spell, "name": "同名技能", "cat": "custom"}
                 for who, spell in [(1, 999001), (1, 999002), (2, 999004)]]
        extras = [999001, 999002, 999004]
        buff, = self.intervals(casts, extras)
        self.assertNotIn("trackSpell", buff)
        # Choosing only one of two same-name casts does not establish an alias.
        buff, = self.intervals(casts, [999001])
        self.assertNotIn("trackSpell", buff)
        buff, = self.intervals(casts, extras, owner=2)
        self.assertEqual(buff["trackSpell"], 999004)


class SpellSelectionValidationTest(unittest.TestCase):
    def test_valid_ids_are_deduplicated_and_sorted(self):
        self.assertEqual(server.parse_mplus_spells("456,123,456"), [123, 456])
        self.assertEqual(server.parse_mplus_spells(iter([456, 123])), [123, 456])
        self.assertEqual(server.parse_mplus_spells(""), [])

    def test_invalid_ids_and_oversized_selection_are_user_errors(self):
        for value in ["0", "-2", "1.5", "123,,456", "x", "1, 2", "9" * 5000, [True], [1.0]]:
            with self.subTest(value=value), self.assertRaises(server.WclError):
                server.parse_mplus_spells(value)
        self.assertEqual(len(server.parse_mplus_spells(range(1, 101))), 100)
        with self.assertRaisesRegex(server.WclError, "100"):
            server.parse_mplus_spells(range(1, 102))

    def test_english_query_preserves_spell_selection_and_restores_language(self):
        handler = server.Handler.__new__(server.Handler)
        handler._json = Mock()
        handler.path = "/api/mplus?code=report&fight=2&lang=en-US&spells=456,123"
        previous_language = server.language()
        with patch.object(server, "api_mplus", side_effect=lambda code, fight, spells: {
                "language": server.language(), "spells": spells}) as api:
            handler.do_GET()
        api.assert_called_once_with("report", 2, [123, 456])
        handler._json.assert_called_once_with({"language": "en", "spells": [123, 456]})
        self.assertEqual(server.language(), previous_language)

    def test_english_query_localizes_invalid_ids_and_limit_errors(self):
        handler = server.Handler.__new__(server.Handler)
        handler._json = Mock()
        for spells, expected in (("0", "positive integer spell IDs"),
                                 (",".join(map(str, range(1, 102))), "100 additional tracked spells")):
            with self.subTest(spells=spells), patch.object(server, "api_mplus") as api:
                handler.path = "/api/mplus?code=report&fight=2&lang=en&spells=" + spells
                handler.do_GET()
                api.assert_not_called()
                self.assertEqual(handler._json.call_args.args[1], 400)
                self.assertIn(expected, handler._json.call_args.args[0]["error"])
                self.assertNotRegex(handler._json.call_args.args[0]["error"], r"[\u4e00-\u9fff]")

    def test_http_route_passes_ids_and_returns_400_for_invalid_selection(self):
        handler = server.Handler.__new__(server.Handler)
        handler._json = Mock()
        handler.path = "/api/mplus?code=report&fight=2&spells=456,123"
        with patch.object(server, "api_mplus", return_value={"ok": True}) as api:
            handler.do_GET()
        api.assert_called_once_with("report", 2, [123, 456])
        handler._json.assert_called_once_with({"ok": True})
        handler._json.reset_mock()
        handler.path = "/api/mplus?code=report&fight=2&spells=0"
        with patch.object(server, "api_mplus") as api:
            handler.do_GET()
        api.assert_not_called()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.assertIn("正整数", handler._json.call_args.args[0]["error"])


if __name__ == "__main__":
    unittest.main()
