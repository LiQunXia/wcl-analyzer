import json
import sys
import tempfile
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import wcl_verify  # noqa: E402


def simple_cookie(name, value, domain, path="/", secure=False):
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["domain"] = domain
    cookie[name]["path"] = path
    cookie[name]["secure"] = secure
    return cookie


class CookieExtractionTest(unittest.TestCase):
    def test_flattens_simple_cookie_and_filters_domain(self):
        cookies = [
            simple_cookie("wcl_session", "abc", ".warcraftlogs.com", secure=True),
            simple_cookie("_ga", "x", ".google-analytics.com"),
        ]
        self.assertEqual(wcl_verify.wcl_cookies(cookies), [{
            "name": "wcl_session",
            "value": "abc",
            "domain": ".warcraftlogs.com",
            "path": "/",
            "secure": True,
        }])

    def test_accepts_dicts_and_keeps_last_value(self):
        cookies = [
            {"name": "wcl_session", "value": "old", "domain": "www.warcraftlogs.com", "path": "/"},
            {"name": "wcl_session", "value": "new", "domain": "www.warcraftlogs.com", "path": "/"},
            {"name": "", "value": "ignored", "domain": "www.warcraftlogs.com", "path": "/"},
        ]
        self.assertEqual([c["value"] for c in wcl_verify.wcl_cookies(cookies)], ["new"])

    def test_empty(self):
        self.assertEqual(wcl_verify.wcl_cookies(None), [])


class ParseResultTest(unittest.TestCase):
    def test_ignores_other_stdout_lines(self):
        stdout = "native log line\n" + wcl_verify.RESULT_PREFIX + '{"cookies": []}\n'
        self.assertEqual(wcl_verify.parse_result(stdout), {"cookies": []})

    def test_missing_payload(self):
        self.assertIsNone(wcl_verify.parse_result("boom"))
        self.assertIsNone(wcl_verify.parse_result(""))
        self.assertIsNone(wcl_verify.parse_result(None))

    def test_bad_json(self):
        self.assertIsNone(wcl_verify.parse_result(wcl_verify.RESULT_PREFIX + "{oops"))


class CookiePersistenceTest(unittest.TestCase):
    def test_roundtrip(self):
        cookies = [{"name": "wcl_session", "value": "abc",
                    "domain": ".warcraftlogs.com", "path": "/", "secure": True}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wcl_cookies.json"
            with mock.patch.object(server, "_wcl_cookie_path", return_value=path):
                server.WclClient()._save_cookies(cookies)
                restored = server.WclClient()
            self.assertEqual(
                restored.session.cookies.get("wcl_session", domain=".warcraftlogs.com"), "abc")
            if path.exists():
                self.assertEqual(json.loads(path.read_text("utf-8")), cookies)


class VerifyCommandTest(unittest.TestCase):
    def test_frozen_uses_the_exe_itself(self):
        with mock.patch.object(server.sys, "frozen", True, create=True), \
             mock.patch.object(server.sys, "executable", "WCL-Analyzer.exe"):
            self.assertEqual(
                server._verify_command("https://x"),
                ["WCL-Analyzer.exe", "--verify", "https://x"],
            )

    def test_dev_uses_the_script(self):
        command = server._verify_command("https://x")
        self.assertEqual(command[0], server.sys.executable)
        self.assertTrue(command[1].endswith("wcl_verify.py"))
        self.assertEqual(command[2], "https://x")


class VerifyWindowTest(unittest.TestCase):
    def test_parses_child_output(self):
        script = "print('noise')\nprint('__WCL_VERIFY__ {\"cookies\": []}')"
        with mock.patch.object(server, "_verify_command",
                               return_value=[sys.executable, "-c", script]):
            self.assertEqual(server._run_verify_window("https://x"), {"cookies": []})

    def test_child_failure_is_reported(self):
        with mock.patch.object(server, "_verify_command",
                               return_value=[sys.executable, "-c", "raise SystemExit(3)"]):
            result = server._run_verify_window("https://x")
        self.assertIn("验证窗口异常退出", result["error"])


class GetJsonTest(unittest.TestCase):
    def make_client(self):
        client = server.WclClient()
        client.verified_at = 0.0
        return client

    def test_other_thread_already_verified(self):
        client = self.make_client()
        responses = [None, {"ok": True}, {"ok": True}]
        with mock.patch.object(client, "_request_json", side_effect=lambda *a: responses.pop(0)), \
             mock.patch.object(server, "_run_verify_window") as verify:
            self.assertEqual(client.get_json("abc", "/x"), {"ok": True})
        verify.assert_not_called()

    def test_runs_window_and_retries(self):
        client = self.make_client()
        responses = [None, None, {"ok": True}]
        cookies = [{"name": "wcl_session", "value": "v",
                    "domain": ".warcraftlogs.com", "path": "/"}]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(client, "_request_json", side_effect=lambda *a: responses.pop(0)), \
             mock.patch.object(server, "_wcl_cookie_path",
                               return_value=Path(tmp) / "wcl_cookies.json"), \
             mock.patch.object(server, "_run_verify_window", return_value={"cookies": cookies}) as verify:
            self.assertEqual(client.get_json("abc", "/x"), {"ok": True})
        verify.assert_called_once()
        self.assertEqual(client.session.cookies.get("wcl_session", domain=".warcraftlogs.com"), "v")

    def test_window_cancelled(self):
        client = self.make_client()
        responses = [None, None]
        with mock.patch.object(client, "_request_json", side_effect=lambda *a: responses.pop(0)), \
             mock.patch.object(server, "_run_verify_window",
                               return_value={"error": "验证窗口已关闭，未完成验证"}):
            with self.assertRaises(server.WclError) as ctx:
                client.get_json("abc", "/x")
        self.assertIn("验证窗口已关闭", str(ctx.exception))

    def test_recent_verification_is_not_repeated(self):
        client = self.make_client()
        client.verified_at = time.time()
        responses = [None, None]
        with mock.patch.object(client, "_request_json", side_effect=lambda *a: responses.pop(0)), \
             mock.patch.object(server, "_run_verify_window") as verify:
            with self.assertRaises(server.WclError) as ctx:
                client.get_json("abc", "/x")
        verify.assert_not_called()
        self.assertIn("刚刚完成验证", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
