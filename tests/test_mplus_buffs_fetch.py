import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class MplusBuffEventsFetchTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        cache_patch = patch.object(server, "CACHE", Path(self.cache.name))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_filtered_pages_use_exact_cursor_and_cache_complete_result(self):
        # The next cursor can share the last event's timestamp; advancing it by
        # one millisecond would lose other events at that same timestamp.
        first = {"timestamp": 150, "type": "applybuff"}
        second = {"timestamp": 150, "type": "applybuff", "targetID": 2}
        with patch.object(server.wcl, "get_json", side_effect=[
            {"events": [first], "nextPageTimestamp": 150},
            {"events": [second]},
        ]) as get_json:
            actual = server.mplus_buff_events("report", 2, 100, 200, [31884, 48707], ['技能 "引号"'])
            self.assertEqual(actual, [first, second])
            self.assertIn("/2/100/200/", get_json.call_args_list[0].args[1])
            self.assertIn("/2/150/200/", get_json.call_args_list[1].args[1])
            pins = parse_qs(urlparse(get_json.call_args_list[0].args[1]).query)["pins"][0]
            self.assertIn("ability.id IN (31884,48707)", pins)
            self.assertIn(json.dumps('技能 "引号"', ensure_ascii=False), pins)
            self.assertEqual(server.mplus_buff_events("report", 2, 100, 200, [48707, 31884], ['技能 "引号"']), actual)
            self.assertEqual(get_json.call_count, 2)

    def test_custom_spell_filter_cache_is_separate_by_language_and_selection(self):
        def fetch(code, path):
            name = server.translate("自选技能", "Custom spell")
            return {"events": [{"ability": {"guid": 999001, "name": name}}]}

        with patch.object(server.wcl, "get_json", side_effect=fetch) as get_json:
            for locale, expected in (("zh-CN", "自选技能"), ("en", "Custom spell"),
                                     ("zh-CN", "自选技能"), ("en", "Custom spell")):
                token = server.LANGUAGE.set(locale)
                try:
                    # Identical IDs and names still require different locale caches.
                    events = server.mplus_buff_events("report", 2, 100, 200, [999001], ["Shared spell name"])
                    self.assertEqual(events[0]["ability"]["name"], expected)
                finally:
                    server.LANGUAGE.reset(token)
            self.assertEqual(get_json.call_count, 2)
            token = server.LANGUAGE.set("en")
            try:
                server.mplus_buff_events("report", 2, 100, 200, [999001, 999002], ["Shared spell name"])
            finally:
                server.LANGUAGE.reset(token)
            self.assertEqual(get_json.call_count, 3)
        paths = list(Path(self.cache.name).rglob("*.json"))
        self.assertEqual(len(paths), 3)
        self.assertEqual({path.parts[-3] for path in paths}, {"zh-CN", "en"})

    def test_failed_page_is_not_cached(self):
        with patch.object(server.wcl, "get_json", side_effect=[
            {"events": [{"timestamp": 150}], "nextPageTimestamp": 150},
            {"events": [], "nextPageTimestamp": 150},
        ]):
            with self.assertRaises(server.WclError):
                server.mplus_buff_events("report", 2, 100, 200, [31884])
        self.assertEqual(list(Path(self.cache.name).rglob("*.json")), [])

    def test_event_at_exact_run_end_is_not_skipped(self):
        event = {"timestamp": 200, "type": "removebuff"}
        with patch.object(server.wcl, "get_json", side_effect=[
            {"events": [], "nextPageTimestamp": 200},
            {"events": [event]},
        ]) as get_json:
            self.assertEqual(server.mplus_buff_events("report", 2, 100, 200, [31884]), [event])
            self.assertIn("/2/200/200/", get_json.call_args_list[1].args[1])

    def test_empty_filter_does_not_fetch_all_buffs(self):
        with patch.object(server.wcl, "get_json") as get_json:
            self.assertEqual(server.mplus_buff_events("report", 2, 100, 200, [], ["broken$pin", "broken^pin"]), [])
            get_json.assert_not_called()

    def test_filter_changes_and_in_progress_runs_do_not_reuse_other_results(self):
        with patch.object(server.wcl, "get_json", return_value={"events": []}) as get_json:
            server.mplus_buff_events("report", 2, 100, 200, [31884])
            server.mplus_buff_events("report", 2, 100, 200, [48707])
            server.mplus_buff_events("report", 2, 100, 300, [31884], persist=False)
            server.mplus_buff_events("report", 2, 100, 300, [31884], persist=False)
            self.assertEqual(get_json.call_count, 4)
        self.assertEqual(len(list(Path(self.cache.name).rglob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
