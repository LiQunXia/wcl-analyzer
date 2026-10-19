"""Run actual page boot and Analyze handlers in Node, without browser/network access.

Run with: .venv/bin/python -m unittest discover -s tests -p 'test_report_boot.py'
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
SAVED_LINK = "https://cn.warcraftlogs.com/reports/SavedReport?fight=3"
SHARED_LINK = "https://www.warcraftlogs.com/reports/SharedReport"

HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = { link: { value: '' }, form: {} };
const calls = [];
const context = vm.createContext({
  URLSearchParams,
  location: { search: input.search },
  sessionStorage: { getItem: key => {
    if (key !== 'wcl.raid.link') throw new Error('Unexpected storage key');
    return input.savedLink;
  } },
  restoredReview: input.restoredReview,
  refreshHistory: () => {},
  $: id => elements[id],
  loadReport: (link, fightId) => calls.push({ link, fightId: fightId ?? null }),
});
vm.runInContext(input.submitHandler, context);
vm.runInContext(input.boot, context);
const bootCalls = calls.slice();
let prevented = false;
elements.form.onsubmit({ preventDefault() { prevented = true; } });
process.stdout.write(JSON.stringify({
  value: elements.link.value, bootCalls, submitCall: calls.at(-1), prevented,
}));
"""


@unittest.skipUnless(NODE, "Node.js is required to execute the page JavaScript regression tests")
class ReportBootTest(unittest.TestCase):
    def run_boot(self, page, search):
        html = (ROOT / page).read_text(encoding="utf-8")
        boot = html.split("/* ---------- boot ---------- */", 1)[1].split("</script>", 1)[0]
        submit_handler = re.search(
            r'\$\("form"\)\.onsubmit = .*?\};', html
        )
        self.assertIsNotNone(submit_handler)
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps({
                "boot": boot,
                "submitHandler": submit_handler.group(),
                "search": search,
                "savedLink": SAVED_LINK,
                "restoredReview": {"link": SAVED_LINK, "code": "SavedReport", "runId": 3},
            }),
            text=True, capture_output=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    def test_shared_raid_report_replaces_saved_input_and_analyze_uses_it(self):
        self.assert_shared_report("index.html")

    def test_shared_mplus_report_replaces_saved_input_and_analyze_uses_it(self):
        self.assert_shared_report("mplus.html")

    def assert_shared_report(self, page):
        for suffix, fight_id in (("", None), ("&f=7", 7)):
            with self.subTest(page=page, suffix=suffix):
                result = self.run_boot(page, "?r=SharedReport" + suffix)
                self.assertEqual(result["value"], SHARED_LINK)
                self.assertEqual(result["bootCalls"], [{"link": SHARED_LINK, "fightId": fight_id}])
                self.assertEqual(result["submitCall"], {"link": SHARED_LINK, "fightId": None})
                self.assertTrue(result["prevented"])

    def test_raid_without_shared_report_restores_input_without_loading(self):
        for search in ("", "?r=&lang=en"):
            with self.subTest(search=search):
                result = self.run_boot("index.html", search)
                self.assertEqual(result["value"], SAVED_LINK)
                self.assertEqual(result["bootCalls"], [])
                self.assertEqual(result["submitCall"], {"link": SAVED_LINK, "fightId": None})

    def test_mplus_without_shared_report_restores_input_and_saved_run(self):
        for search in ("", "?r=&lang=en"):
            with self.subTest(search=search):
                result = self.run_boot("mplus.html", search)
                self.assertEqual(result["value"], SAVED_LINK)
                self.assertEqual(result["bootCalls"], [{
                    "link": "https://www.warcraftlogs.com/reports/SavedReport", "fightId": 3,
                }])
                self.assertEqual(result["submitCall"], {"link": SAVED_LINK, "fightId": None})


if __name__ == "__main__":
    unittest.main()
