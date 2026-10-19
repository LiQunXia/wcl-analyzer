"""Execute the actual Mythic+ spell settings helpers and handlers offline in Node.

Run: .venv/bin/python -m unittest discover -s tests -p 'test_mplus_spell_settings_ui.py'
"""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const writes = [];
const requests = [];
function element(id) {
  return elements[id] ||= {
    value: '', scrollTop: 37, innerHTML: '', textContent: '', hidden: false,
    disabled: false, open: false, focused: false, listeners: {},
    focus() { this.focused = true; },
    showModal() { this.open = true; }, close() { this.open = false; },
    addEventListener(event, handler) { this.listeners[event] = handler; },
    querySelector() { return null; },
  };
}
const context = vm.createContext({
  URL, URLSearchParams, structuredClone,
  window: {},
  location: { search: '?lang=' + input.lang, href: 'http://localhost/mplus' },
  document: { documentElement: {} },
  localStorage: {
    getItem: key => key === 'wcl.mplus.spellPrefs.v1' ? input.saved : null,
    setItem: (key, value) => writes.push({key, value}),
  },
  $: element,
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  iconUrl: icon => '/api/icon/' + icon,
  stopPlay() {}, renderDetail() {}, renderPullList() {}, showMsg() {},
  api: async path => { requests.push(path); return input.data; },
});
vm.runInContext(input.i18n, context);
vm.runInContext(`const { t } = window.I18N; const I18N = window.I18N;` + input.categories, context);
vm.runInContext(`const state = ${JSON.stringify(input.state)};
state.cats = new Set(state.cats); state.selected = new Set(state.selected);
const D = () => state.data;`, context);
vm.runInContext(input.helpers + input.derived + input.settings, context);
(async () => {
  const result = await vm.runInContext('(async () => {' + input.action + '})()', context);
  process.stdout.write(JSON.stringify({result, writes, requests}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def section(source, start, end):
    return start + source.split(start, 1)[1].split(end, 1)[0]


@unittest.skipUnless(NODE, "Node.js is required for the spell settings UI regression tests")
class MplusSpellSettingsUITest(unittest.TestCase):
    def run_js(self, action, *, lang="en", saved=None, prefs=None, options=None):
        html = (ROOT / "mplus.html").read_text(encoding="utf-8")
        data = {
            "spellOptions": options if options is not None else [
                {"spell": 10, "name": "Native burst", "icon": "burst", "cat": "offensive", "count": 2},
                {"spell": 20, "name": "Native buff", "icon": "buff", "cat": "defensive", "count": 0},
                {"spell": 30, "name": "Extra spell", "icon": "extra", "cat": "custom", "count": 1},
                {"spell": 50, "name": "Default spell", "icon": "default", "cat": "offensive", "count": 1},
            ],
            "trackedSpells": [30],
        }
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps({
                "i18n": (ROOT / "i18n.js").read_text(encoding="utf-8"),
                "categories": section(html, "const CATS =", "const CAT_COLOR"),
                "helpers": section(html, "const SPELL_PREFS_KEY =", "const state ="),
                "derived": section(html, "const spellVisible =", "function pullIndexAt"),
                "settings": section(html, "/* ---------- per-spell monitoring ---------- */", "function renderMatrix()"),
                "state": {"data": data, "cats": ["offensive"], "selected": [], "spellPrefs": prefs or {}, "code": "Report", "runId": 7},
                "lang": lang, "saved": saved, "data": data, "action": action,
            }),
            text=True, capture_output=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    def test_visibility_category_overrides_alias_and_player_filter(self):
        result = self.run_js(r"""
          const cast = {spell: 10, cat: 'offensive', who: 1};
          const checks = [spellVisible(cast), spellVisible({...cast, cat: 'custom'})];
          state.spellPrefs = {10: {mode: 'hide'}, 30: {mode: 'show'}};
          checks.push(spellVisible(cast), spellVisible({...cast, spell: 30, cat: 'custom'}));
          checks.push(spellVisible({...cast, spell: 11, trackSpell: 10}));
          checks.push(spellVisible({...cast, trackSpell: 999}));
          state.spellPrefs[10] = {mode: 'show'};
          state.selected = new Set([2]);
          checks.push(visibleCast(cast), visibleCast({...cast, who: 2}));
          state.selected.clear(); checks.push(visibleCast(cast));
          return checks;
        """)["result"]
        self.assertEqual(result, [True, False, False, True, False, False, False, True, True])

    def test_pull_casts_merge_chronologically_without_mutating_original(self):
        result = self.run_js(r"""
          const pull = {casts: [{t: 9}, {t: 1}], extraCasts: [{t: 5}]};
          return {merged: pullCasts(pull).map(c => c.t), original: pull.casts.map(c => c.t),
            nativeOnly: pullCasts({casts: [{t: 2}, {t: 1}]}).map(c => c.t)};
        """)["result"]
        self.assertEqual(result, {"merged": [1, 5, 9], "original": [9, 1], "nativeOnly": [1, 2]})

    def test_saved_preferences_normalize_invalid_entries_and_query(self):
        saved = json.dumps({
            "10": {"mode": "show", "name": "Burst", "cat": "offensive", "icon": "burst", "lang": "en"},
            "20": {"mode": "hide", "name": 7, "cat": "unknown", "icon": False},
            "30": {"mode": "show"}, "0": {"mode": "show"}, "-1": {"mode": "show"},
            "01": {"mode": "show"}, "9007199254740992": {"mode": "show"},
            "40": {"mode": "other"}, "50": None,
        })
        result = self.run_js("const prefs = readSpellPrefs(); return {prefs, ids: extraSpellIds(prefs), query: spellQuery(prefs), empty: spellQuery({})};", saved=saved)["result"]
        self.assertEqual(set(result["prefs"]), {"10", "20", "30"})
        self.assertEqual(result["prefs"]["10"], {"mode": "show", "name": "Burst", "cat": "offensive", "icon": "burst", "lang": "en"})
        self.assertEqual(result["prefs"]["20"]["name"], "Spell #20")
        self.assertEqual(result["prefs"]["20"]["cat"], "custom")
        self.assertEqual(result["prefs"]["20"]["icon"], "")
        self.assertEqual(result["ids"], [10, 30])
        self.assertEqual(result["query"], "&spells=10,30")
        self.assertEqual(result["empty"], "")
        for invalid in ("{broken", "null", "[]", '"text"'):
            with self.subTest(saved=invalid):
                self.assertEqual(self.run_js("return readSpellPrefs();", saved=invalid)["result"], {})

    def test_dynamic_dialog_in_both_languages_and_all_statuses(self):
        prefs = {
            "10": {"mode": "show", "name": "Stale name", "cat": "custom"},
            "20": {"mode": "hide", "name": "Native buff", "cat": "defensive"},
            "40": {"mode": "show", "name": "Absent spell", "cat": "custom", "icon": "absent"},
        }
        for lang in ("en", "zh-CN"):
            with self.subTest(lang=lang):
                result = self.run_js(r"""
                  openSpellSettings();
                  const all = $('spellList').innerHTML;
                  const count = $('spellSettingsCount').textContent;
                  const options = spellOptions(spellDraft);
                  $('spellSearch').value = 'does not exist'; $('spellSearch').oninput();
                  const empty = $('spellList').innerHTML;
                  $('spellSearch').value = ''; $('spellView').value = 'hidden'; $('spellView').onchange();
                  const hidden = $('spellList').innerHTML;
                  return {all, count, empty, hidden, options};
                """, lang=lang, prefs=prefs)["result"]
                self.assertEqual([o["spell"] for o in result["options"]].count(10), 1)
                self.assertEqual(next(o for o in result["options"] if o["spell"] == 10)["name"], "Native burst")
                self.assertFalse(next(o for o in result["options"] if o["spell"] == 40)["available"])
                self.assertIn("Native buff", result["hidden"])
                self.assertNotIn("Native burst", result["hidden"])
                self.assertIn('data-reset-spell="10"', result["all"])
                if lang == "en":
                    self.assertNotRegex(result["all"] + result["count"] + result["empty"], r"[\u4e00-\u9fff]")
                    self.assertRegex(result["all"], r"[Cc]ast.*2|2.*[Cc]ast")
                    self.assertRegex(result["all"], r"[Rr]eset|[Dd]efault")
                    for label in ("Always shown", "Hidden", "Not monitored", "Follow category", "Buff records", "No records in this run"):
                        self.assertIn(label, result["all"])
                    self.assertIn("Always shown 2", result["count"])
                    self.assertIn("Hidden 1", result["count"])
                    self.assertIn("No matching spells", result["empty"])
                else:
                    self.assertIn("始终显示", result["all"])
                    self.assertIn("已隐藏", result["count"])
                    self.assertIn("没有匹配", result["empty"])

    def test_individual_reset_and_new_custom_spell_apply(self):
        prefs = {"10": {"mode": "hide", "name": "Native burst", "cat": "offensive"}}
        result = self.run_js(r"""
          openSpellSettings();
          $('spellList').onclick({target: {closest: () => ({dataset: {resetSpell: '10'}})}});
          const reset = structuredClone(spellDraft);
          $('spellList').onchange({target: {dataset: {spell: '30'}, checked: true}});
          state.data.trackedSpells = [];
          await $('spellApply').onclick();
          return {reset, prefs: state.spellPrefs, open: spellSettings.open};
        """, prefs=prefs)
        self.assertEqual(result["result"]["reset"], {})
        self.assertEqual(result["result"]["prefs"]["30"]["mode"], "show")
        self.assertEqual(result["result"]["prefs"]["30"]["cat"], "custom")
        self.assertFalse(result["result"]["open"])
        self.assertEqual(result["requests"], ["/api/mplus?code=Report&fight=7&spells=30"])
        saved = next(w for w in result["writes"] if w["key"] == "wcl.mplus.spellPrefs.v1")
        self.assertEqual(json.loads(saved["value"]), result["result"]["prefs"])

    def test_cancel_discards_draft_and_apply_persists_reset(self):
        prefs = {"10": {"mode": "hide", "name": "Native burst", "cat": "offensive"}}
        result = self.run_js(r"""
          openSpellSettings();
          $('spellList').onchange({target: {dataset: {spell: '20'}, checked: true}});
          closeSpellSettings();
          const afterCancel = structuredClone(state.spellPrefs);
          openSpellSettings();
          const reopened = structuredClone(spellDraft);
          $('spellReset').onclick();
          await $('spellApply').onclick();
          return {afterCancel, reopened, final: state.spellPrefs, open: spellSettings.open,
            disabled: $('spellControls').disabled};
        """, prefs=prefs)
        self.assertEqual(result["result"]["afterCancel"], prefs)
        self.assertEqual(result["result"]["reopened"], prefs)
        self.assertEqual(result["result"]["final"], {})
        self.assertFalse(result["result"]["open"])
        self.assertFalse(result["result"]["disabled"])
        self.assertEqual(result["requests"], [])
        writes = [w for w in result["writes"] if w["key"] == "wcl.mplus.spellPrefs.v1"]
        self.assertEqual(writes, [{"key": "wcl.mplus.spellPrefs.v1", "value": "{}"}])


if __name__ == "__main__":
    unittest.main()
