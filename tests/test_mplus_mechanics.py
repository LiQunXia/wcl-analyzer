import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server


class MechanicTest(unittest.TestCase):
    def setUp(self):
        self.fight = {"id": 2, "start_time": 100000, "end_time": 200000,
                      "dungeonPulls": [{"id": 7, "start_time": 100000, "end_time": 200000,
                                        "enemies": [[99, 9, 1, 2]]}]}
        self.players = {1: {"id": 1, "type": "Warrior", "icon": "Warrior-Arms"},
                        2: {"id": 2, "type": "Shaman", "icon": "Shaman-Restoration"}}
        self.enemies = {9: {"id": 9, "name": "怪物"}}
        self.rule = {"cast": 1306852, "damage": [1306853], "window": 12}
        self.config = server.load_mplus_mechanics()

    def event(self, second, spell, kind="damage", source=9, target=1, **extra):
        return {"timestamp": 100000 + round(second * 1000), "type": kind,
                "sourceID": source, "targetID": target,
                "ability": {"guid": spell, "name": f"技能{spell}", "abilityIcon": "test.jpg"},
                **extra}

    def cast(self, second, instance=1):
        return self.event(second, 1306852, "cast", sourceInstance=instance)

    def rounds(self, casts, events, auras=()):
        return server.mplus_mechanic_rounds(self.fight, self.players, self.enemies, casts,
            self.rule, {"events": events, "auras": auras}, self.config)

    def test_graph_pairs_keep_completion_and_skip_cancelled_begin(self):
        begin = self.event(1, 1306852, "begincast")
        done = self.cast(3)
        instant = self.event(6, 108271, "cast", source=2)
        data = {"series": [{"events": [[begin, done], [begin, {}], [instant, {}], instant]}]}
        self.assertEqual(server.complete_casts(data), [done, instant, instant])

    def test_overlapping_monster_instances_and_repeated_casts_stay_separate(self):
        casts = [self.cast(10, 1), self.cast(11, 2), self.cast(20, 1)]
        events = [self.event(12, 1306853, sourceInstance=1, amount=100),
                  self.event(12, 1306853, sourceInstance=2, amount=200),
                  self.event(21, 1306853, sourceInstance=1, amount=300),
                  self.event(22, 1306853, sourceInstance=3, amount=999)]
        rounds, unmatched = self.rounds(casts, events)
        self.assertEqual([r["damage"] for r in rounds], [100, 200, 300])
        self.assertEqual([r["number"] for r in rounds], [1, 2, 3])
        self.assertEqual(unmatched, 1)

    def test_group_simultaneous_hits_and_keep_zero_damage_and_missing_snapshots(self):
        events = [self.event(10, 1306853, amount=0, buffs="23920.385391.114052.", absorbed=50),
                  self.event(10.008, 1306853, target=2, amount=200),
                  self.event(14, 1306853, amount=300)]
        rounds, _ = self.rounds([self.cast(10)], events,
                               [{"guid": 23920, "name": "法术反射"}])
        r = rounds[0]
        self.assertEqual((len(r["pulses"]), r["hits"], r["targets"]), (2, 3, 2))
        self.assertEqual((r["damage"], r["absorbed"]), (500, 50))
        hits = r["pulses"][0]["players"]
        self.assertEqual(len(hits[0]["buffs"]), 1)
        self.assertEqual(hits[1]["buffs"], [])
        self.assertEqual(r["pulses"][1]["players"][0]["buffs"], [])

    def test_pet_targets_and_damage_before_cast_or_after_window_are_excluded(self):
        events = [self.event(9.999, 1306853, amount=100),
                  self.event(23, 1306853, amount=100),
                  self.event(11, 1306853, target=99, amount=100)]
        rounds, unmatched = self.rounds([self.cast(10)], events)
        self.assertEqual(rounds[0]["hits"], 0)
        self.assertEqual(unmatched, 2)

    def test_detail_distinguishes_exact_absorb_pet_healing_balance_and_cast_phases(self):
        hit = self.event(10, 1306853, amount=100, absorbed=20)
        occurrence = self.rounds([self.cast(10)], [hit])[0][0]
        events = [hit,
            self.event(10, 1307578, "absorbed", source=2, amount=20,
                       attackerID=9, extraAbility={"guid": 1306853}),
            self.event(10, 1307578, "absorbed", source=2, amount=900,
                       attackerID=9, attackerInstance=2, extraAbility={"guid": 1306853}),
            self.event(10, 1307578, "absorbed", source=2, amount=800,
                       attackerID=9, extraAbility={"guid": 1}),
            self.event(11, 1267745, "heal", source=20, amount=50, overheal=10),
            self.event(10, 98021, "damage", source=20, amount=600),
            self.event(11, 98021, "heal", source=20, amount=600),
            self.event(10, 1, amount=100),
            self.event(10.2, 1, "heal", source=2, amount=0, resourceActor=2,
                       hitPoints=50, maxHitPoints=100),
            self.event(10.3, 1, "heal", source=2, amount=0, resourceActor=2,
                       hitPoints=20, maxHitPoints=100)]
        casts = [self.event(9, 98008, "cast", source=2),
                 self.event(10, 108271, "cast", source=2),
                 self.event(11, 1267068, "cast", source=2)]
        deaths = [self.event(10.5, 0, "death"), self.event(11, 0, "death", target=2, feign=True)]
        meta = {"friendlyPets": [{"id": 20, "petOwner": 2}]}
        detail = server.mplus_response_detail(meta, self.fight, self.players, occurrence,
                                             self.rule, events, casts, deaths)
        self.assertTrue(detail["replayMatched"])
        self.assertEqual(detail["allDamage"], 200)
        self.assertEqual(detail["absorbs"][0]["amount"], 20)
        heal = next(h for h in detail["healing"] if h["spell"] == 1267745)
        self.assertEqual((heal["who"], heal["amount"], heal["overheal"]), (2, 50, 10))
        balance = next(h for h in detail["healing"] if h["spell"] == 98021)
        self.assertEqual((balance["kind"], balance["amount"], balance["events"]), ("balance", 0, 2))
        self.assertEqual([c["phase"] for c in detail["responses"]], ["before", "during", "after"])
        self.assertEqual(detail["health"][1], [[10.3, 20.0]])
        self.assertEqual(len(detail["deaths"]), 1)

    def test_same_hit_count_with_wrong_damage_is_not_marked_verified(self):
        hit = self.event(10, 1306853, amount=100)
        occurrence = self.rounds([self.cast(10)], [hit])[0][0]
        changed = {**hit, "amount": 101}
        result = server.mplus_response_detail({}, self.fight, self.players, occurrence,
                                             self.rule, [changed], [], [])
        self.assertFalse(result["replayMatched"])

    def test_delayed_damage_uses_first_hit_for_preparation_phase(self):
        hit = self.event(11.5, 1306853, amount=100)
        occurrence = self.rounds([self.cast(10)], [hit])[0][0]
        result = server.mplus_response_detail({}, self.fight, self.players, occurrence,
            self.rule, [hit], [self.event(11, 98008, "cast", source=2)], [])
        self.assertEqual(result["responses"][0]["phase"], "before")


class MechanicFetchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)
        p = patch.object(server, "CACHE", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def test_exact_page_cursor_and_aura_names_are_retained(self):
        pages = [{"events": [{"timestamp": 150}], "nextPageTimestamp": 150,
                  "auraAbilities": [{"guid": 97463, "name": "集结呐喊"}]},
                 {"events": [{"timestamp": 150, "targetID": 2}]}]
        with patch.object(server.wcl, "get_json", side_effect=pages) as fetch:
            result = server.mplus_event_data("report", 2, 100, 200, 1306853)
            self.assertEqual(len(result["events"]), 2)
            self.assertEqual(result["auras"][0]["name"], "集结呐喊")
            self.assertIn("/2/150/200/", fetch.call_args_list[1].args[1])
            self.assertEqual(server.mplus_event_data("report", 2, 100, 200, 1306853), result)
            self.assertEqual(fetch.call_count, 2)

    def test_partial_or_stalled_pages_are_never_cached(self):
        with patch.object(server.wcl, "get_json", side_effect=[
            {"events": [], "nextPageTimestamp": 150},
            {"events": [], "nextPageTimestamp": 150},
        ]):
            with self.assertRaises(server.WclError):
                server.mplus_event_data("report", 2, 100, 200, 1306853)
        self.assertFalse(list(self.cache.rglob("*.json")))

    def test_unrecognised_occurrence_never_fetches_replay(self):
        with patch.object(server, "api_mplus_mechanics", return_value={"mechanics": []}), \
                patch.object(server.wcl, "get_json") as fetch:
            with self.assertRaises(server.WclError):
                server.api_mplus_mechanic("report", 2, "unknown")
            fetch.assert_not_called()

    def test_mechanic_catalog_is_packaged_and_has_unique_positive_ids(self):
        config = server.load_mplus_mechanics()
        self.assertEqual(len({r["cast"] for r in config["mechanics"]}), len(config["mechanics"]))
        self.assertTrue(all(r["window"] > 0 and r["damage"] for r in config["mechanics"]))
        self.assertIn('ROOT / "mplus_mechanics.json"', (server.ROOT / "wcl.spec").read_text())


if __name__ == "__main__":
    unittest.main()
