import contextlib
import json
import re
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


@contextlib.contextmanager
def use_language(value):
    token = server.LANGUAGE.set(server.normalize_language(value))
    try:
        yield
    finally:
        server.LANGUAGE.reset(token)


class LanguageTest(unittest.TestCase):
    def test_accepts_browser_and_service_locale_aliases(self):
        for expected, aliases in (
            ("zh-CN", ("zh", "zh-CN", "zhCN", "zh_cn", "ZH-cn")),
            ("en", ("en", "en-US", "enUS", "en_us", "EN-us")),
        ):
            for value in aliases:
                with self.subTest(value=value):
                    self.assertEqual(server.normalize_language(value), expected)

    def test_missing_and_unsupported_locales_keep_chinese_default(self):
        for value in (None, "", "fr", "../../en", "<script>"):
            with self.subTest(value=value):
                self.assertEqual(server.normalize_language(value), "zh-CN")

    def test_nested_language_contexts_restore_the_previous_language(self):
        with use_language("zh"):
            self.assertEqual(server.translate("未知怪物", "Unknown enemy"), "未知怪物")
            with use_language("en"):
                self.assertEqual(server.language(), "en")
                self.assertEqual(server.translate("未知怪物", "Unknown enemy"), "Unknown enemy")
            self.assertEqual(server.language(), "zh-CN")

    def test_fight_difficulty_uses_request_language(self):
        fight = {"id": 1, "name": "Encounter", "difficulty": 5,
                 "start_time": 0, "end_time": 1500}
        with use_language("zh"):
            self.assertEqual(server.fmt_fight(fight)["difficulty"], "史诗")
        with use_language("en"):
            self.assertEqual(server.fmt_fight(fight)["difficulty"], "Mythic")

    def test_invalid_report_link_has_english_validation_message(self):
        with use_language("en"), self.assertRaises(server.WclError) as error:
            server.parse_link("invalid-report-link")
        self.assertIn("https://www.warcraftlogs.com/reports/", str(error.exception))
        self.assertIsNone(re.search(r"[\u4e00-\u9fff]", str(error.exception)))


class LocalizedCacheTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        patcher = mock.patch.object(server, "CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_report_cache_name_has_independent_locale_contents(self):
        calls = []

        def fetch():
            calls.append(server.language())
            return {"name": server.translate("怪物", "Enemy"), "guid": 123}

        for locale, expected in (("zh", "怪物"), ("en", "Enemy"),
                                 ("zh", "怪物"), ("en", "Enemy")):
            with use_language(locale):
                self.assertEqual(server.cached("report/1_enemies.json", fetch),
                                 {"name": expected, "guid": 123})
        self.assertEqual(calls, ["zh-CN", "en"])
        self.assertEqual(len(list(self.cache.rglob("*.json"))), 2)

    def test_legacy_chinese_cache_is_not_reused_as_english_data(self):
        legacy = self.cache / "report" / "1_enemies.json"
        legacy.parent.mkdir()
        legacy.write_text('{"name":"旧中文"}', "utf-8")
        with use_language("en"):
            actual = server.cached("report/1_enemies.json", lambda: {"name": "Enemy"})
        self.assertEqual(actual, {"name": "Enemy"})

    def test_nonpersistent_results_do_not_create_locale_cache_files(self):
        with use_language("en"):
            self.assertEqual(server.cached("live/report.json", lambda: {"ok": True}, False),
                             {"ok": True})
        self.assertEqual(list(self.cache.rglob("*.json")), [])

    def test_report_metadata_does_not_mix_same_report_between_languages(self):
        def fetch(code, path):
            return {"code": code, "name": server.translate("地下城", "Dungeon")}

        with mock.patch.object(server, "_meta_cache", {}), \
             mock.patch.object(server.wcl, "get_json", side_effect=fetch) as get_json:
            for locale, expected in (("zh", "地下城"), ("en", "Dungeon"),
                                     ("zh", "地下城"), ("en", "Dungeon")):
                with use_language(locale):
                    self.assertEqual(server.report_meta("same-report")["name"], expected)
            self.assertEqual(get_json.call_count, 2)


class WclLocaleRequestTest(unittest.TestCase):
    def test_wcl_request_preserves_filters_and_sends_locale_per_request(self):
        client = server.WclClient.__new__(server.WclClient)
        client.session = mock.Mock()
        client.session.headers = {"User-Agent": "test-agent"}
        client.session.get.return_value = mock.Mock(
            headers={"content-type": "application/json"}, text='{"ok": true}', status_code=200)
        pins = 'ability.name IN ("技能", "Spell")'
        path = "/reports/events/test?" + urlencode({"pins": pins, "lang": "cn"})

        for locale, upstream_locale, header in (("zh", "cn", "zh-CN"), ("en", "en", "en")):
            with self.subTest(locale=locale), use_language(locale):
                self.assertEqual(client._request_json(path, "https://www.warcraftlogs.com/reports/test"),
                                 {"ok": True})
                call = client.session.get.call_args
                query = parse_qs(urlparse(call.args[0]).query)
                self.assertEqual(query["pins"], [pins])
                self.assertEqual(query["lang"], [upstream_locale])
                self.assertTrue(call.kwargs["headers"]["Accept-Language"].startswith(header))
                self.assertIn("/reports/test", call.kwargs["headers"]["Referer"])
        self.assertEqual(client.session.headers, {"User-Agent": "test-agent"})


class SpellLocaleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for target, value in (
            ("CACHE", self.cache),
            ("_spells", {"zh-CN": {}, "en": {}}),
            ("_spell_files", {"zh-CN": self.cache / "spells-zh.json",
                              "en": self.cache / "spells-en.json"}),
        ):
            patcher = mock.patch.object(server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_workers_fetch_and_cache_spell_names_in_the_requested_locale(self):
        calls = []

        def fetch(url, **kwargs):
            locale = parse_qs(urlparse(url).query)["locale"][0]
            spell_id = urlparse(url).path.rsplit("/", 1)[-1]
            calls.append((spell_id, locale))
            name = ("技能" if locale == "4" else "Spell ") + spell_id
            return mock.Mock(json=lambda: {"name": name, "icon": "same_icon"})

        with mock.patch.object(server.requests, "get", side_effect=fetch):
            for locale, prefix in (("zh", "技能"), ("en", "Spell "),
                                   ("zh", "技能"), ("en", "Spell ")):
                with use_language(locale):
                    actual = server.spell_info([1234, 1235])
                    self.assertEqual(actual[1234], {"name": prefix + "1234", "icon": "same_icon"})
                    self.assertEqual(actual[1235]["name"], prefix + "1235")
        self.assertCountEqual(calls, [("1234", "4"), ("1235", "4"),
                                      ("1234", "0"), ("1235", "0")])
        files = list(self.cache.rglob("*.json"))
        self.assertEqual(len(files), 2)
        names = {json.loads(path.read_text("utf-8"))["1234"]["name"] for path in files}
        self.assertEqual(names, {"技能1234", "Spell 1234"})

    def test_unavailable_english_spell_does_not_reuse_cached_chinese_name(self):
        server._spells["zh-CN"]["1234"] = {"name": "中文技能", "icon": "same_icon"}
        with use_language("en"), \
             mock.patch.object(server.requests, "get", return_value=mock.Mock(json=lambda: {})):
            self.assertEqual(server.spell_info([1234]), {1234: {}})


class QuietHandler(server.Handler):
    def log_message(self, fmt, *args):
        pass


class LanguageRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def json_request(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def test_report_route_propagates_query_and_uses_chinese_by_default(self):
        with mock.patch.object(server, "api_report", side_effect=lambda link: {
            "link": link, "language": server.language(),
        }):
            self.assertEqual(self.json_request("/api/report?lang=en-US&link=example"),
                             {"link": "example", "language": "en"})
            self.assertEqual(self.json_request("/api/report?link=example")["language"], "zh-CN")
            self.assertEqual(self.json_request("/api/report?lang=invalid")["language"], "zh-CN")

    def test_simultaneous_chinese_and_english_requests_remain_isolated(self):
        rendezvous = threading.Barrier(2)

        def report(link):
            first = server.language()
            rendezvous.wait(timeout=3)
            return {"initial": first, "final": server.language()}

        with mock.patch.object(server, "api_report", side_effect=report), \
             ThreadPoolExecutor(2) as pool:
            chinese = pool.submit(self.json_request, "/api/report?lang=zh")
            english = pool.submit(self.json_request, "/api/report?lang=en")
            self.assertEqual(chinese.result(), {"initial": "zh-CN", "final": "zh-CN"})
            self.assertEqual(english.result(), {"initial": "en", "final": "en"})

    def test_handler_restores_context_even_when_an_api_raises(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/report?lang=en"
        handler._json = mock.Mock()
        with use_language("zh"), \
             mock.patch.object(server, "api_report", side_effect=server.WclError("problem")):
            handler.do_GET()
            self.assertEqual(server.language(), "zh-CN")
        handler._json.assert_called_once_with({"error": "problem"}, 400)

    def test_both_pages_and_shared_language_script_are_served(self):
        for path in ("/", "/mplus"):
            with self.subTest(path=path), urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertIn("i18n.js", response.read().decode("utf-8"))
        with urlopen(self.base + "/i18n.js", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers["Content-Type"])
            self.assertTrue(response.read())


if __name__ == "__main__":
    unittest.main()
