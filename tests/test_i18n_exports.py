"""Offline locale checks for exported notes and the verification helper.

Run with: .venv/bin/python -m unittest discover -s tests -p 'test_i18n_exports.py'
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import localization
import mdt_export
import wcl_verify


class EnglishExportTest(unittest.TestCase):
    def setUp(self):
        token = localization.LANGUAGE.set("en")
        self.addCleanup(localization.LANGUAGE.reset, token)

    def test_mdt_annotations_survive_import_string_round_trip_in_english(self):
        enemies = {
            1: {"id": 1001, "count": 60, "clones": {1: {"x": 1, "y": -1, "g": 1}}},
            2: {"id": 1002, "count": 20, "clones": {1: {"x": 30, "y": -2, "g": 2}}},
        }
        pulls = [{"id": 10, "forcesPct": 80, "enemies": [
            {"guid": 1001, "engaged": 1, "killed": 1},
            {"guid": 1002, "engaged": 1, "killed": 1},
        ]}]
        tank_route = {"pulls": [{"id": 10, "start": 0, "steps": [
            {"kind": "join", "label": "A", "t": 0, "members": [{"guid": 1001, "name": "First enemy"}]},
            {"kind": "stop", "t": 8, "until": 20},
            {"kind": "join", "label": "B", "t": 30, "chain": True, "alive": 1,
             "members": [{"guid": 1002, "name": "Second enemy"}],
             "recentDeaths": [{"name": "First enemy"}]},
        ]}]}
        dungeon = {"index": 999, "key": "TestDungeon", "names": [], "enemies": enemies}
        with mock.patch.object(mdt_export, "_match_dungeon", return_value=dungeon), \
             mock.patch.object(mdt_export, "load_dungeons", return_value=[dungeon]):
            result = mdt_export.build_route(Path("."), {"name": "Test Dungeon", "level": 20,
                                                       "countRequired": 100}, pulls,
                                            tank_route=tank_route)
        decoded = mdt_export.decode_mdt2(result["string"])
        text = "\n".join(obj["d"][4] for obj in decoded["objects"] if obj.get("n"))
        self.assertIn("WCL pull 1", text)
        self.assertIn("Tank stops for about 12s", text)
        self.assertIn("Chain pull", text)
        self.assertIn("enemies from previous batches are still alive", text)
        self.assertIn("deaths in the previous 8s", text)
        self.assertEqual(result["arrows"], 1)
        self.assertNotRegex(text + result["annotation_notice"], r"[\u3400-\u9fff]")

    def test_mdt_errors_use_request_language(self):
        with self.assertRaisesRegex(mdt_export.MdtError, "Not an MDT2 import string"):
            mdt_export.decode_mdt2("invalid")
        with self.assertRaisesRegex(mdt_export.MdtError, "No dungeon matches"):
            mdt_export._match_dungeon([], "missing dungeon")


class EnglishVerificationTest(unittest.TestCase):
    def setUp(self):
        token = localization.LANGUAGE.set("en")
        self.addCleanup(localization.LANGUAGE.reset, token)

    def test_window_title_and_creation_error_use_english(self):
        webview = SimpleNamespace(create_window=mock.Mock(return_value=None))
        with mock.patch.dict(sys.modules, {"webview": webview}):
            result = wcl_verify.run("https://www.warcraftlogs.com/reports/example")
        self.assertEqual(result["error"], "Unable to create the verification window")
        self.assertIn("WCL verification", webview.create_window.call_args.args[0])
        self.assertNotRegex(webview.create_window.call_args.args[0], r"[\u3400-\u9fff]")

    def test_watcher_keeps_language_when_started_on_another_thread(self):
        window = SimpleNamespace(events=SimpleNamespace(closed=mock.MagicMock()), destroy=mock.Mock())

        def start(watch, **_kwargs):
            worker = threading.Thread(target=watch)
            worker.start()
            worker.join()

        webview = SimpleNamespace(create_window=mock.Mock(return_value=window), renderer="mshtml", start=start)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(sys.modules, {"webview": webview}), \
             mock.patch.object(wcl_verify, "_storage_path", return_value=Path(tmp)):
            result = wcl_verify.run("https://www.warcraftlogs.com/reports/example")
        self.assertIn("The WebView2 runtime is missing", result["error"])
        self.assertNotRegex(result["error"], r"[\u3400-\u9fff]")
        window.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
