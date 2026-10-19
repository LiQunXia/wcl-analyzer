"""Exercise the route map's real JavaScript in Node, without browser/network access.

Run: .venv/bin/python -m unittest discover -s tests -p 'test_map_zoom.py'
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
const map = { id: 1, minX: 0, maxX: 100, minY: 0, maxY: 100 };
const elements = {
  map: { clientWidth: 900, clientHeight: 600, dataset: { w: 1500, h: 1000 },
    innerHTML: '', setAttribute() {}, querySelectorAll() { return []; } },
  mapbox: { clientWidth: 900, clientHeight: 600 },
  liveDots: { innerHTML: '' },
};
const state = {
  floor: 1, pullIdx: 0, focus: false, showMobs: true, showTracks: false,
  tankStep: null, selected: new Set(), t: 0, view: [0, 0, 1500, 1000],
  paths: { mobs: [], tracks: {} },
};
const data = { pulls: [], players: [], deaths: [] };
let tank = null;
const frames = [];
const context = vm.createContext({
  state, elements, data, map, frames,
  $: id => elements[id], D: () => data, mapById: () => map,
  tankPull: () => tank, setTank: value => { tank = value; },
  curPull: () => data.pulls[state.pullIdx],
  playerById: () => Object.fromEntries(data.players.map(p => [p.id, p])),
  posAt: (track, t) => track?.filter(p => p.t <= t).at(-1),
  tankId: () => 1, tankStepText: () => 'Step',
  CLASS_COLOR: { Warrior: '#c79c6e' },
  t: (zh, en) => en,
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  signed: String, mmss: String,
  pullColor: () => '#abcdef', pullLabel: i => `Pull ${i + 1}`,
  mobIconUrl: icon => icon, listSeparator: ', ',
  I18N: { apiUrl: url => url }, renderMapNow() {}, hideTip() {},
  requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
  cancelAnimationFrame() {},
});
vm.runInContext(input.source, context);
vm.runInContext('imgSize[1] = [1500, 1000]', context);
Promise.resolve(vm.runInContext('(async () => {' + input.scenario + '})()', context))
  .then(result => process.stdout.write(JSON.stringify(result)))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "Node.js is required for map JavaScript regression tests")
class MapZoomTest(unittest.TestCase):
    def run_map(self, scenario):
        html = (ROOT / "mplus.html").read_text(encoding="utf-8")
        source = html.split("/* ---------- map ---------- */", 1)[1].split('$("zoomIn").onclick', 1)[0]
        result = subprocess.run(
            [NODE, "-e", HARNESS], input=json.dumps({"source": source, "scenario": scenario}),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_clusters_expand_without_losing_enemies_or_merging_pulls(self):
        result = self.run_map(r"""
          state.paths.mobs = [
            { pull: 0, floor: 1, x: 50, y: 50, guid: 1 },
            { pull: 0, floor: 1, x: 50.2, y: 50, guid: 2 },
            { pull: 0, floor: 1, x: 50, y: 50, guid: 1 },
            { pull: 1, floor: 1, x: 50, y: 50, guid: 3 },
            { pull: 0, floor: 2, x: 50, y: 50, guid: 4 },
          ];
          const summarize = groups => groups.map(g => ({
            pull: g.pull, n: g.n, parts: [...g.parts.values()].reduce((n, p) => n + p.n, 0),
          }));
          const low = summarize(mobMapGroups(map, 1500, 1000, mapMetrics(1500, 1000)));
          state.view = [600, 400, 200, 1000 / 7.5];
          const high = summarize(mobMapGroups(map, 1500, 1000, mapMetrics(1500, 1000)));
          state.focus = true;
          const focused = summarize(mobMapGroups(map, 1500, 1000, mapMetrics(1500, 1000)));
          return { low, high, focused };
        """)
        self.assertEqual(sorted((g["pull"], g["n"]) for g in result["low"]), [(0, 3), (1, 1)])
        self.assertEqual(len(result["high"]), 4)
        self.assertEqual(len(result["focused"]), 3)
        for level in result.values():
            self.assertTrue(all(g["parts"] == g["n"] for g in level))
        self.assertEqual(sum(g["n"] for g in result["high"]), 4)
        self.assertTrue(all(g["pull"] == 0 for g in result["focused"]))

    def test_coincident_detail_icons_keep_distinct_centers_and_original_anchors(self):
        result = self.run_map(r"""
          state.view = [650, 400, 200, 400 / 3];
          state.paths.mobs = Array.from({ length: 5 }, (_, guid) => ({
            pull: 0, floor: 1, x: 50, y: 50, guid,
          }));
          return mobMapGroups(map, 1500, 1000, mapMetrics(1500, 1000))
            .map(g => ({ x: g.x, y: g.y, anchorX: g.anchorX, anchorY: g.anchorY }));
        """)
        self.assertEqual(len(result), 5)
        self.assertEqual(len({(g["x"], g["y"]) for g in result}), 5)
        self.assertTrue(all((g["anchorX"], g["anchorY"]) == (750, 500) for g in result))

    def test_boss_does_not_merge_with_trash_at_same_location(self):
        result = self.run_map(r"""
          state.paths.mobs = [
            { pull: 0, floor: 1, x: 50, y: 50, guid: 1, boss: false },
            { pull: 0, floor: 1, x: 50, y: 50, guid: 2, boss: true },
          ];
          return mobMapGroups(map, 1500, 1000, mapMetrics(1500, 1000))
            .map(g => ({ n: g.n, bosses: [...g.parts.values()].map(p => !!p.boss) }));
        """)
        self.assertEqual(len(result), 2)
        self.assertEqual(sorted(g["bosses"] for g in result), [[False], [True]])
        self.assertTrue(all(g["n"] == 1 for g in result))

    def test_screen_scale_is_bounded_for_wide_and_narrow_viewports(self):
        result = self.run_map(r"""
          const samples = [];
          for (const [width, height] of [[360, 800], [1800, 500]]) {
            elements.map.clientWidth = width; elements.map.clientHeight = height;
            for (const view of [[0, 0, 1500, 1000], [0, 0, 100, 100]]) {
              state.view = view;
              const metrics = mapMetrics(1500, 1000);
              samples.push({ screenScale: metrics.scale * metrics.pixelsPerUnit,
                detail: metrics.detail, ppu: metrics.pixelsPerUnit });
            }
          }
          return samples;
        """)
        for sample in result:
            self.assertGreaterEqual(sample["screenScale"], 1 - 1e-9)
            self.assertLessEqual(sample["screenScale"], 1.25 + 1e-9)
            self.assertEqual(sample["detail"], sample["ppu"] >= 1.8)

    def test_live_player_radius_stays_capped_after_zoom_and_replay_time_change(self):
        result = self.run_map(r"""
          data.players = [{ id: 1, cls: 'Warrior', name: 'Tank' }];
          state.paths.tracks[1] = [
            { t: 0, floor: 1, x: 40, y: 50 }, { t: 10, floor: 1, x: 60, y: 50 },
          ];
          const sample = () => {
            drawLive();
            const circle = elements.liveDots.innerHTML;
            return { radius: +circle.match(/ r="([^"]+)"/)[1] * mapMetrics(1500, 1000).pixelsPerUnit,
              x: +circle.match(/ cx="([^"]+)"/)[1] };
          };
          const before = sample();
          zoomMap(0.1);
          state.t = 10;
          return { before, after: sample(), scheduled: frames.length };
        """)
        self.assertNotEqual(result["before"]["x"], result["after"]["x"])
        self.assertGreaterEqual(result["scheduled"], 1)
        for key in ("before", "after"):
            self.assertGreater(result[key]["radius"], 0)
            self.assertLessEqual(result[key]["radius"], 8.75 + 1e-9)

    def test_tank_stop_folding_is_stable_when_zoom_changes(self):
        result = self.run_map(r"""
          data.pulls = [{ start: 0 }];
          setTank({ segments: [], steps: [
            { kind: 'join', map: 1, x: 50, y: 50, t: 0, label: 'A' },
            { kind: 'stop', map: 1, x: 50.8, y: 50, t: 2, until: 8 },
          ] });
          const samples = [];
          for (const width of [1500, 100]) {
            state.view = [0, 0, width, width / 1.5];
            const markup = tankMapAnnotations(map, 1500, 1000, mapMetrics(1500, 1000), []);
            samples.push({ stopMarkers: (markup.match(/class="tank-mark stop"/g) || []).length,
              foldedStop: markup.includes('S1') });
          }
          return samples;
        """)
        self.assertEqual(result, [{"stopMarkers": 0, "foldedStop": True}] * 2)

    def test_refit_and_scheduled_zoom_redraw_use_the_new_view(self):
        result = self.run_map(r"""
          state.view = [700, 450, 100, 200 / 3];
          data.pulls = [{ map: 1, x: 50, y: 50, start: 0, end: 30, name: 'Pack' }];
          state.paths.mobs = [
            { pull: 0, floor: 1, x: 50, y: 50, guid: 1 },
            { pull: 0, floor: 1, x: 50.2, y: 50, guid: 2 },
          ];
          const sample = () => {
            const markup = elements.map.innerHTML;
            const circles = [...markup.matchAll(/class="mob-marker"[^>]*>\s*(?:<line[^>]*>\s*)?<circle[^>]* r="([^"]+)"/g)];
            return { count: circles.length,
              radii: circles.map(c => +c[1] * mapMetrics(1500, 1000).pixelsPerUnit),
              width: state.view[2] };
          };
          await drawMap(true);
          const fitted = sample();
          zoomMap(0.1);
          frames.shift()();
          await Promise.resolve();
          return { fitted, zoomed: sample() };
        """)
        self.assertGreater(result["fitted"]["width"], 100)
        self.assertEqual(result["fitted"]["count"], 1)
        self.assertEqual(result["zoomed"]["count"], 2)
        self.assertLess(result["zoomed"]["width"], result["fitted"]["width"])
        for sample in result.values():
            for radius in sample["radii"]:
                self.assertGreaterEqual(radius, 10.4 - 1e-9)
                self.assertLessEqual(radius, 13 + 1e-9)


if __name__ == "__main__":
    unittest.main()
