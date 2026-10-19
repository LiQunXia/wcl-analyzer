"""Offline checks: .venv/bin/python -m unittest discover -s tests"""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server


class TankRouteTest(unittest.TestCase):
    def setUp(self):
        self.fight = {
            "id": 1, "start_time": 100000, "end_time": 180000,
            "npcCountMap": {"100": 5, "200": 5, "300": 0},
            "dungeonPulls": [{"id": 10, "start_time": 100000, "end_time": 180000,
                              "maps": [5], "enemies": [[100, 11, 1, 2], [200, 12, 1, 1], [300, 13, 1, 1]]}],
            "dungeonReplay": {"maps": [{"mapID": 5, "mapMinX": 0, "mapMaxX": 20000,
                                         "mapMinY": 0, "mapMaxY": 20000}]},
        }
        self.meta = {"friendlies": [{"id": 1, "type": "DeathKnight", "icon": "DeathKnight-Blood", "fights": ".1."}],
                     "enemies": [{"id": 11, "guid": 100}, {"id": 12, "guid": 200}, {"id": 13, "guid": 300}]}
        self.paths = {
            "tracks": {"1": [[t, 1000, 1000, 5] for t in range(0, 24, 2)]
                       + [[t, 5000, 5000, 5] for t in range(30, 60, 2)]},
            "contacts": [self.contact(11, 1, 1), self.contact(11, 2, 3), self.contact(12, 1, 30)],
            "enemyDeaths": [{"actor": 11, "instance": 1, "t": 28}, {"actor": 11, "instance": 2, "t": 45}],
        }

    def contact(self, actor, instance, t):
        return {"actor": actor, "instance": instance, "guid": {11: 100, 12: 200, 13: 300}[actor],
                "name": f"怪物{actor}", "who": 1, "spell": "测试命中", "outgoing": True, "t": t}

    def analyse(self):
        return server.mplus_tank_route(self.fight, self.meta, self.paths)["pulls"]

    def test_join_stay_and_chain_with_recorded_death_context(self):
        p = self.analyse()[0]
        joins = [s for s in p["steps"] if s["kind"] == "join"]
        self.assertEqual([len(s["members"]) for s in joins], [2, 1])
        self.assertTrue(joins[1]["chain"])
        self.assertEqual(joins[1]["alive"], 1)
        self.assertEqual(joins[1]["recentDeaths"], [{"name": "怪物11", "t": 28}])
        self.assertEqual(len([s for s in p["steps"] if s["kind"] == "stop"]), 2)

    def test_missing_death_does_not_prove_survival(self):
        self.paths["enemyDeaths"] = []
        joins = [s for s in self.analyse()[0]["steps"] if s["kind"] == "join"]
        self.assertFalse(joins[1]["chain"])
        self.assertEqual(joins[1]["alive"], 0)

    def test_missing_coordinates_do_not_invent_positions_or_bridge_gaps(self):
        self.paths["contacts"][2]["t"] = 26
        p = self.analyse()[0]
        second = [s for s in p["steps"] if s["kind"] == "join"][1]
        self.assertNotIn("x", second)
        self.assertEqual(len(p["segments"]), 2)

    def test_floor_change_splits_route_and_uses_explicit_map_id(self):
        self.fight["dungeonPulls"][0]["maps"] = [5, 6]
        self.paths["tracks"]["1"] = [[t, 1000, 1000, 5 if t < 8 else 6] for t in range(0, 16, 2)]
        p = self.analyse()[0]
        self.assertEqual([s[0]["map"] for s in p["segments"]], [5, 6])
        self.assertFalse(any(s["kind"] == "stop" for s in p["steps"]))

    def test_explicit_floor_outside_pull_does_not_bridge_overlapping_maps(self):
        other_floor = dict(self.fight["dungeonReplay"]["maps"][0], mapID=6)
        self.fight["dungeonReplay"]["maps"].append(other_floor)
        self.paths["tracks"]["1"] = [
            [t, 1000, 1000, 6 if 6 <= t < 18 else 5] for t in range(0, 24, 2)
        ]
        p = self.analyse()[0]
        self.assertEqual([[q["t"] for q in s] for s in p["segments"]],
                         [[0, 2, 4], [18, 20, 22]])
        self.assertTrue(all(q["map"] == 5 for s in p["segments"] for q in s))

    def test_explicit_floor_outside_pull_keeps_join_without_coordinates_or_stop(self):
        other_floor = dict(self.fight["dungeonReplay"]["maps"][0], mapID=6)
        self.fight["dungeonReplay"]["maps"].append(other_floor)
        self.paths["tracks"]["1"] = [[t, 1000, 1000, 6] for t in range(0, 14, 2)]
        self.paths["contacts"] = [self.contact(11, 1, 6)]
        p = self.analyse()[0]
        self.assertEqual(p["observed"], 1)
        self.assertEqual(len(p["steps"]), 1)
        join = p["steps"][0]
        self.assertEqual(join["kind"], "join")
        self.assertEqual(join["members"], self.paths["contacts"])
        for key in ("x", "y", "map"):
            self.assertNotIn(key, join)
        self.assertEqual(p["segments"], [])

    def test_missing_floor_still_infers_unique_pull_map_from_coordinates(self):
        for floor in ([], [None]):
            with self.subTest(floor=floor):
                self.paths["tracks"]["1"] = [[t, 1000, 1000] + floor for t in range(0, 14, 2)]
                self.paths["contacts"] = [self.contact(11, 1, 6)]
                p = self.analyse()[0]
                self.assertEqual(len(p["segments"]), 1)
                self.assertTrue(all(q["map"] == 5 for q in p["segments"][0]))
                self.assertEqual({s["kind"] for s in p["steps"]}, {"join", "stop"})
                for step in p["steps"]:
                    self.assertEqual((step["x"], step["y"], step["map"]), (1000, 1000, 5))

    def test_one_enemy_is_not_reintroduced_when_wcl_splits_a_chain(self):
        one = self.fight["dungeonPulls"][0]
        one["end_time"] = 125000
        two = copy.deepcopy(one)
        two.update(id=20, start_time=125000, end_time=180000)
        self.fight["dungeonPulls"].append(two)
        a, b = self.analyse()
        self.assertEqual(a["observed"], 2)
        self.assertEqual(b["observed"], 1)
        self.assertTrue(next(s for s in b["steps"] if s["kind"] == "join")["chain"])

    def test_zero_count_summons_and_bosses_do_not_create_trash_chain_steps(self):
        self.paths["contacts"].append(self.contact(13, 1, 50))
        self.assertEqual(self.analyse()[0]["observed"], 3)
        self.fight["dungeonPulls"][0]["boss"] = 999
        self.assertEqual(self.analyse()[0]["steps"], [])

    def test_no_tank_does_not_substitute_a_dps(self):
        self.meta["friendlies"][0]["icon"] = "DeathKnight-Frost"
        self.assertIsNone(server.mplus_tank_route(self.fight, self.meta, self.paths)["tank"])


class ReplayInteractionsTest(unittest.TestCase):
    def test_first_damage_not_first_sighting_and_instances_are_retained(self):
        fight = {"id": 1, "boss": 1, "start_time": 100000, "end_time": 110000}
        meta = {"friendlies": [{"id": 1, "type": "DeathKnight", "fights": ".1."}],
                "enemies": [{"id": 11, "guid": 100, "name": "怪物"}]}
        events = [
            {"timestamp": 100000, "type": "applybuff", "sourceID": 11, "sourceInstance": 2,
             "resourceActor": 1, "x": 10, "y": 20, "mapID": 5},
            {"timestamp": 101000, "type": "cast", "sourceID": 1, "targetID": 11,
             "targetInstance": 2, "targetIsFriendly": False},
            {"timestamp": 102000, "type": "damage", "sourceID": 1, "targetID": 11,
             "targetInstance": 2, "targetIsFriendly": False, "ability": {"name": "命中"}},
            {"timestamp": 104000, "type": "damage", "sourceID": 1, "targetID": 11,
             "targetInstance": 3, "targetIsFriendly": False},
            {"timestamp": 109000, "type": "death", "targetID": 11, "targetInstance": 2,
             "targetIsFriendly": False},
        ]
        with patch.object(server.wcl, "get_json", return_value={"events": events}):
            p = server._extract_paths("test", fight, meta)
        self.assertEqual([(c["instance"], c["t"]) for c in p["contacts"]], [(2, 2), (3, 4)])
        self.assertEqual(p["mobs"][0]["instance"], 2)
        self.assertEqual(p["enemyDeaths"], [{"actor": 11, "instance": 2, "t": 9}])


if __name__ == "__main__":
    unittest.main()
