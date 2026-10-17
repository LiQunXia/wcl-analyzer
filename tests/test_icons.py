import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, content=b"", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class QuietHandler(server.Handler):
    def log_message(self, fmt, *args):
        pass


class IconTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.icon_dir = Path(self.tmp.name) / "icons"
        for target, value in (("ICON_DIR", self.icon_dir), ("_icon_locks", {}), ("_icon_misses", {})):
            patcher = mock.patch.object(server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class NormalizeIconTest(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(server.normalize_icon("spell_holy_holybolt"), "spell_holy_holybolt")

    def test_strips_wcl_prefix_and_extension(self):
        self.assertEqual(
            server.normalize_icon("custom-icon-inv_misc_questionmark.jpg"), "inv_misc_questionmark"
        )

    def test_lowercases(self):
        self.assertEqual(server.normalize_icon("Spell_Holy_HolyBolt"), "spell_holy_holybolt")

    def test_rejects_unusable_names(self):
        for bad in ("", None, "  ", "../etc/passwd", "spell/abc", "a b", "图标"):
            with self.subTest(bad=bad):
                self.assertEqual(server.normalize_icon(bad), "")


class IconFetchTest(IconTestBase):
    def test_downloads_once_then_serves_from_disk(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return FakeResponse(content=b"\xff\xd8" + b"x" * 200)

        with mock.patch.object(server.requests, "get", fake_get):
            first = server.icon_bytes("spell_holy_holybolt")
            second = server.icon_bytes("spell_holy_holybolt")

        self.assertTrue(first.startswith(b"\xff\xd8"))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.icon_dir / "spell_holy_holybolt.jpg").exists())

    def test_falls_back_to_next_upstream(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "rpglogs" in url:
                return FakeResponse(403, b"blocked", "text/html")
            return FakeResponse(content=b"\xff\xd8" + b"y" * 200)

        with mock.patch.object(server.requests, "get", fake_get):
            data = server.icon_bytes("spell_holy_holybolt")

        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertEqual(
            calls[0], "https://assets.rpglogs.com/img/warcraft/abilities/spell_holy_holybolt.jpg"
        )
        self.assertIn("render.worldofwarcraft.com", calls[1])

    def test_all_upstreams_failing_backs_off(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return FakeResponse(403, b"nope", "text/html")

        with mock.patch.object(server.requests, "get", fake_get):
            self.assertIsNone(server.icon_bytes("spell_holy_holybolt"))
            self.assertIsNone(server.icon_bytes("spell_holy_holybolt"))

        self.assertEqual(len(calls), len(server.ICON_UPSTREAMS))
        self.assertFalse((self.icon_dir / "spell_holy_holybolt.jpg").exists())

    def test_network_errors_are_skipped(self):
        def fake_get(url, **kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(server.requests, "get", fake_get):
            self.assertIsNone(server.icon_bytes("spell_holy_holybolt"))

    def test_invalid_name_never_hits_network(self):
        def fake_get(url, **kwargs):
            raise AssertionError("should not fetch")

        with mock.patch.object(server.requests, "get", fake_get):
            self.assertIsNone(server.icon_bytes("../etc/passwd"))


class IconMimeTest(unittest.TestCase):
    def test_sniffs_common_formats(self):
        self.assertEqual(server.icon_mime(b"\xff\xd8\xff\xe0" + b"0" * 16), "image/jpeg")
        self.assertEqual(server.icon_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 16), "image/png")
        self.assertEqual(server.icon_mime(b"GIF89a" + b"0" * 16), "image/gif")
        self.assertEqual(server.icon_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_placeholder_is_svg(self):
        self.assertTrue(server.ICON_PLACEHOLDER.startswith(b"<svg"))


class IconRouteTest(IconTestBase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def test_serves_icon_with_long_cache(self):
        payload = b"\xff\xd8" + b"z" * 200
        with mock.patch.object(server, "icon_bytes", lambda name: payload):
            with urlopen(f"{self.base}/api/icon/spell_holy_holybolt") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers["Content-Type"], "image/jpeg")
                self.assertIn("max-age=604800", r.headers["Cache-Control"])
                self.assertEqual(r.read(), payload)

    def test_serves_uncached_placeholder_for_missing_icon(self):
        with mock.patch.object(server, "icon_bytes", lambda name: None):
            with urlopen(f"{self.base}/api/icon/missing_icon") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
                self.assertEqual(r.headers["Cache-Control"], "no-store")
                self.assertTrue(r.read().startswith(b"<svg"))


class PageIconWiringTest(unittest.TestCase):
    def test_pages_use_local_icon_proxy(self):
        for name in ("index.html", "mplus.html"):
            with self.subTest(page=name):
                html = (server.ROOT / name).read_text("utf-8")
                self.assertNotIn("wow.zamimg.com/images/wow/icons", html)
                self.assertIn("/api/icon/", html)


if __name__ == "__main__":
    unittest.main()
