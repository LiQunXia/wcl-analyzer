import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class ClassifyCastTest(unittest.TestCase):
    def setUp(self):
        self.by_class, self.common = server.load_mplus_spells()

    def classify(self, guid, name, cls):
        return server.classify_cast(guid, name, cls, self.by_class, self.common)

    def test_class_cooldowns(self):
        self.assertEqual(self.classify(31884, "复仇之怒", "Paladin"), "offensive")
        self.assertEqual(self.classify(118038, "剑在人在", "Warrior"), "defensive")
        # a spell from another class's list is not attributed to this class
        self.assertIsNone(self.classify(31884, "复仇之怒", "Warrior"))

    def test_global_categories(self):
        self.assertEqual(self.classify(32182, "英勇", "Shaman"), "lust")
        self.assertEqual(self.classify(6552, "拳击", "Warrior"), "interrupt")
        self.assertEqual(self.classify(61999, "复活盟友", "DeathKnight"), "rez")

    def test_potions_by_name(self):
        self.assertEqual(self.classify(999001, "某种新药水", "Mage"), "potion")
        self.assertEqual(self.classify(999002, "浓缩银月城生命药水", "Mage"), "healPotion")
        self.assertIsNone(self.classify(999003, "法力药水", "Mage"))
        self.assertIsNone(self.classify(999004, "寒冰箭", "Mage"))

    def test_catalog_is_valid_json_with_numeric_ids(self):
        data = json.loads((server.ROOT / "mplus_spells.json").read_text("utf-8"))
        for key, cats in data.items():
            if key.startswith("_说明"):
                continue
            for ids in cats.values():
                self.assertTrue(all(isinstance(i, int) for i in ids), key)


class RoleTest(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(server.player_role("DeathKnight-Blood"), "tank")
        self.assertEqual(server.player_role("Shaman-Restoration"), "healer")
        self.assertEqual(server.player_role("Hunter-BeastMastery"), "dps")


class BuffIntervalsTest(unittest.TestCase):
    def setUp(self):
        self.start, self.end = 100000, 160000
        self.players = {
            1: {"id": 1, "name": "战士", "type": "Warrior"},
            2: {"id": 2, "name": "骑士", "type": "Paladin"},
            3: {"id": 3, "name": "另一位骑士", "type": "Paladin"},
        }

    def event(self, kind, second, source=1, target=1, spell=118038,
              name="剑在人在", **extra):
        return {
            "type": kind, "timestamp": self.start + second * 1000,
            "sourceID": source, "targetID": target,
            "sourceIsFriendly": True, "targetIsFriendly": True,
            "ability": {"guid": spell, "name": name, "abilityIcon": "ability_warrior_parry.jpg"},
            **extra,
        }

    def intervals(self, events, casts=None):
        return server.mplus_buff_intervals(events, casts or [], self.players,
                                           self.start, self.end)

    def test_apply_remove_uses_actual_times_and_recipient(self):
        buffs = self.intervals([
            self.event("removebuff", 13.75),
            self.event("applybuff", 2.125),
        ])
        self.assertEqual(buffs, [{
            "t": 2.125, "end": 13.75, "who": 1, "source": 1,
            "spell": 118038, "name": "剑在人在", "icon": "ability_warrior_parry",
            "cat": "defensive", "startKnown": True, "endKnown": True,
        }])

    def test_refresh_and_stack_changes_keep_one_continuous_interval(self):
        events = [self.event(kind, sec) for kind, sec in [
            ("applybuff", 1), ("refreshbuff", 5), ("applybuffstack", 6),
            ("removebuffstack", 8), ("refreshbuff", 12), ("removebuff", 18),
        ]]
        buffs = self.intervals(events)
        self.assertEqual([(b["t"], b["end"]) for b in buffs], [(1, 18)])

    def test_reapplications_keep_inactive_gap(self):
        events = [self.event(kind, sec) for kind, sec in [
            ("applybuff", 1), ("removebuff", 4),
            ("applybuff", 9), ("removebuff", 15),
        ]]
        self.assertEqual([(b["t"], b["end"]) for b in self.intervals(events)],
                         [(1, 4), (9, 15)])

    def test_external_buff_is_attributed_to_recipient_and_caster(self):
        events = [self.event(kind, sec, source=2, target=1, spell=6940, name="牺牲祝福")
                  for kind, sec in [("applybuff", 3), ("removebuff", 15)]]
        buff, = self.intervals(events)
        self.assertEqual((buff["who"], buff["source"], buff["cat"]),
                         (1, 2, "defensive"))
        self.assertEqual((buff["t"], buff["end"]), (3, 15))

    def test_different_casters_of_same_buff_close_independently(self):
        events = [self.event(kind, sec, source=source, target=1, spell=6940, name="牺牲祝福")
                  for kind, sec, source in [
                      ("applybuff", 1, 2), ("applybuff", 3, 3),
                      ("removebuff", 7, 2), ("removebuff", 12, 3),
                  ]]
        self.assertEqual([(b["source"], b["t"], b["end"]) for b in self.intervals(events)],
                         [(2, 1, 7), (3, 3, 12)])

    def test_first_removal_means_active_at_run_start(self):
        buff, = self.intervals([self.event("removebuff", 9)])
        self.assertEqual((buff["t"], buff["end"], buff["startKnown"], buff["endKnown"]),
                         (0, 9, False, True))

    def test_first_refresh_or_stack_means_active_at_run_start(self):
        for kind in ("refreshbuff", "applybuffstack", "removebuffstack"):
            with self.subTest(kind=kind):
                buff, = self.intervals([self.event(kind, 3), self.event("removebuff", 10)])
                self.assertEqual((buff["t"], buff["end"], buff["startKnown"]),
                                 (0, 10, False))

    def test_missing_removal_is_clipped_at_run_end(self):
        buff, = self.intervals([self.event("applybuff", 50)])
        self.assertEqual((buff["t"], buff["end"], buff["startKnown"], buff["endKnown"]),
                         (50, 60, True, False))

    def test_duplicate_removals_do_not_invent_extra_intervals(self):
        events = [self.event(kind, sec) for kind, sec in [
            ("applybuff", 2), ("removebuff", 8), ("removebuff", 9),
            ("applybuff", 15), ("removebuff", 20), ("removebuff", 21),
        ]]
        self.assertEqual([(b["t"], b["end"]) for b in self.intervals(events)],
                         [(2, 8), (15, 20)])
        self.assertEqual(len(self.intervals([
            self.event("removebuff", 8), self.event("removebuff", 9),
        ])), 1)

    def test_unclassified_hostile_and_nonplayer_auras_are_excluded(self):
        events = [
            self.event("applybuff", 1, target=99),
            self.event("applybuff", 1, target=100, targetIsFriendly=False),
            self.event("applybuff", 1, source=99, sourceIsFriendly=False),
            self.event("applybuff", 1, spell=999999, name="普通增益"),
            self.event("applydebuff", 1),
        ]
        self.assertEqual(self.intervals(events), [])

    def test_same_name_cast_matches_a_different_buff_spell_id(self):
        casts = [{"t": 4, "who": 1, "spell": 118038,
                  "name": "剑在人在", "cat": "defensive", "icon": "ability_warrior_parry"}]
        events = [self.event("applybuff", 4.1, spell=999888),
                  self.event("removebuff", 11.2, spell=999888)]
        buff, = self.intervals(events, casts)
        self.assertEqual((buff["spell"], buff["cat"], buff["t"], buff["end"]),
                         (999888, "defensive", 4.1, 11.2))

    def test_nested_npc_source_cannot_match_a_players_cast_name(self):
        casts = [{"who": 2, "spell": 1022, "name": "保护祝福", "cat": "defensive"}]
        event = self.event("applybuff", 2, target=2, spell=999888, name="保护祝福")
        event.pop("sourceID")
        event["source"] = {"id": 38, "type": "NPC"}
        self.assertEqual(self.intervals([event], casts), [])

    def test_owned_pet_source_uses_owner_class_and_cast_name(self):
        casts = [{"who": 2, "spell": 1022, "name": "保护祝福", "cat": "defensive"}]
        events = []
        for kind, second in [("applybuff", 2), ("removebuff", 8)]:
            event = self.event(kind, second, target=1, spell=999888, name="保护祝福")
            event.pop("sourceID")
            event["source"] = {"id": 38, "type": "Pet"}
            events.append(event)
        buff, = server.mplus_buff_intervals(events, casts, self.players, self.start, self.end,
                                            pet_owners={38: 2})
        self.assertEqual((buff["who"], buff["source"], buff["cat"], buff["t"], buff["end"]),
                         (1, 2, "defensive", 2, 8))

    def test_removal_without_source_closes_unambiguous_aura_once(self):
        remove = self.event("removebuff", 8)
        remove.pop("sourceID")
        duplicate = {**remove, "timestamp": self.start + 9000}
        buffs = self.intervals([self.event("applybuff", 2), remove, duplicate])
        self.assertEqual([(b["source"], b["t"], b["end"]) for b in buffs], [(1, 2, 8)])

    def test_later_event_identifies_missing_application_source(self):
        apply = self.event("applybuff", 2)
        apply.pop("sourceID")
        buffs = self.intervals([apply, self.event("removebuff", 8)])
        self.assertEqual([(b["source"], b["t"], b["end"]) for b in buffs], [(1, 2, 8)])

    def test_ambiguous_removal_without_source_does_not_invent_third_caster(self):
        events = [self.event(kind, second, source=source, spell=2825, name="嗜血")
                  for kind, second, source in [
                      ("applybuff", 1, 2), ("applybuff", 2, 3),
                      ("removebuff", 5, None), ("removebuff", 10, 2), ("removebuff", 12, 3),
                  ]]
        events[2].pop("sourceID")
        self.assertEqual([(b["source"], b["t"], b["end"]) for b in self.intervals(events)],
                         [(2, 1, 10), (3, 2, 12)])

    def test_cross_run_intervals_are_clipped_without_outside_auras(self):
        events = [self.event(kind, sec) for kind, sec in [
            ("applybuff", -20), ("removebuff", -10),
            ("applybuff", -3), ("removebuff", 8),
            ("applybuff", 55), ("removebuff", 65),
            ("applybuff", 70), ("removebuff", 75),
        ]]
        self.assertEqual([(b["t"], b["end"], b["startKnown"], b["endKnown"])
                          for b in self.intervals(events)],
                         [(0, 8, False, True), (55, 60, True, False)])


class MplusBuffApiTest(unittest.TestCase):
    def test_run_keeps_actual_buff_interval_across_pull_boundaries(self):
        start, end = 100000, 160000
        meta = {
            "fights": [{
                "id": 2, "start_time": start, "end_time": end, "inProgress": True,
                "name": "测试副本", "dungeonPulls": [
                    {"id": 1, "start_time": start + 10000, "end_time": start + 20000},
                    {"id": 2, "start_time": start + 30000, "end_time": start + 40000},
                ],
            }],
            "friendlies": [{"id": 1, "name": "战士", "type": "Warrior", "fights": ".2.",
                            "icon": "Warrior-Protection"}],
            "enemies": [],
        }
        cast = {"type": "cast", "sourceID": 1, "timestamp": start + 5000,
                "ability": {"abilityIcon": "ability_warrior_parry.jpg"}}
        cast_graph = {"series": [{"guid": 118038, "name": "剑在人在", "events": [cast]}]}
        # A different aura ID is matched to the tracked cast by name, and its
        # lifetime survives the idle gap and both pull boundaries.
        buff_events = [{
            "type": kind, "sourceID": 1, "targetID": 1, "timestamp": start + second * 1000,
            "ability": {"guid": 999888, "name": "剑在人在", "abilityIcon": "ability_warrior_parry.jpg"},
        } for kind, second in [("applybuff", 5.2), ("removebuff", 42.3)]]
        with (patch.object(server, "report_meta", return_value=meta),
              patch.object(server, "graph", side_effect=[cast_graph, {}, {}]),
              patch.object(server, "source_graph", return_value={}),
              patch.object(server, "mplus_buff_events", return_value=buff_events) as fetch_buffs):
            data = server.api_mplus("report", 2)
        buff, = data["buffs"]
        self.assertEqual((buff["t"], buff["end"], buff["spell"], buff["cat"]),
                         (5.2, 42.3, 999888, "defensive"))
        self.assertEqual(data["pulls"][0]["casts"][0]["t"], 5)
        self.assertEqual(data["pulls"][1]["casts"], [])
        self.assertEqual(data["players"][0]["usage"][0]["times"], [5])
        fetch_args = fetch_buffs.call_args
        self.assertEqual(fetch_args.args[:4], ("report", 2, start, end))
        self.assertIn(118038, fetch_args.args[4])
        self.assertIn("剑在人在", fetch_args.args[5])
        self.assertFalse(fetch_args.kwargs["persist"])


if __name__ == "__main__":
    unittest.main()
