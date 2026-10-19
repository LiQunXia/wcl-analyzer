import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class DamageEventParseTest(unittest.TestCase):
    def setUp(self):
        self.start = 100000
        self.players = {1: {"id": 1, "name": "猎人"}}

    def event(self, kind, second, target=1, spell=1216570, name="邪能飞弹", **extra):
        return {
            "timestamp": self.start + second * 1000,
            "type": kind,
            "sourceID": 73,
            "sourceIsFriendly": False,
            "targetID": target,
            "targetIsFriendly": True,
            "ability": {"guid": spell, "name": name},
            **extra,
        }

    def test_zero_damage_hit_is_treated_as_an_immunity(self):
        events = server.mplus_damage_events([self.event("damage", 3)], self.players, self.start)
        self.assertEqual(events[0]["kind"], "immune")
        self.assertEqual(events[0]["who"], 1)
        self.assertEqual(events[0]["t"], 3)

    def test_fully_mitigated_hit_keeps_its_unmitigated_amount(self):
        event = self.event("damage", 3, amount=0, mitigated=120000, unmitigatedAmount=120000)
        parsed, = server.mplus_damage_events([event], self.players, self.start)
        self.assertEqual(parsed["kind"], "mitigated")
        self.assertEqual(parsed["unmitigated"], 120000)

    def test_miss_immune_and_dot_are_classified(self):
        events = [
            self.event("miss", 1, missType="Immune"),
            self.event("damage", 2, amount=5000, tick=True),
            self.event("damage", 3, amount=5000),
        ]
        parsed = server.mplus_damage_events(events, self.players, self.start)
        self.assertEqual([p["kind"] for p in parsed], ["immune", "dot", "damage"])

    def test_events_targeting_other_actors_are_ignored(self):
        self.assertEqual(server.mplus_damage_events(
            [self.event("damage", 1, target=2)], self.players, self.start), [])


class DefensivePurposeTest(unittest.TestCase):
    def setUp(self):
        self.start = 0
        self.players = {
            1: {"id": 1, "name": "骑士", "icon": "Paladin-Retribution", "type": "Paladin"},
            2: {"id": 2, "name": "坦克", "icon": "Warrior-Protection", "type": "Warrior"},
        }

    def damage(self, second, who=1, kind="damage", amount=100000, unmitigated=None, **extra):
        return {"t": second, "who": who, "name": "邪能飞弹", "kind": kind,
                "source": 73, "source_friendly": False, "amount": amount,
                "unmitigated": unmitigated, **extra}

    def test_immune_hit_is_preferred_over_nearby_damage(self):
        events = [self.damage(9.5), self.damage(10.4, kind="immune", amount=0, unmitigated=150000)]
        purpose = server.defensive_purpose(1, 10, events, [], [], self.start)
        self.assertEqual((purpose["kind"], purpose["name"], purpose["delta"]),
                         ("immune", "邪能飞弹", 0.4))
        self.assertEqual(purpose["unmitigated"], 150000)

    def test_reactive_cast_still_matches_the_hit_just_before_it(self):
        events = [self.damage(9.2)]
        purpose = server.defensive_purpose(1, 10, events, [], [], self.start)
        self.assertEqual(purpose["kind"], "damage")
        self.assertEqual(purpose["delta"], -0.8)

    def test_debuff_removal_is_the_strongest_signal(self):
        start = 100000
        debuffs = [{
            "timestamp": start + 10200, "type": "removedebuff", "targetID": 1,
            "ability": {"guid": 1, "name": "流血"},
        }]
        purpose = server.defensive_purpose(1, 10, [self.damage(10.5)], debuffs, [], start)
        self.assertEqual((purpose["kind"], purpose["name"]), ("debuff", "流血"))

    def test_nearby_enemy_cast_is_used_when_no_damage_is_recorded(self):
        casts = [{"t": 10.3, "who": 1, "name": "邪能光束", "source": 73}]
        purpose = server.defensive_purpose(1, 10, [], [], casts, self.start,
                                           {73: "末日守卫"})
        self.assertEqual((purpose["kind"], purpose["source"]), ("enemy_cast", "末日守卫"))

    def test_ground_targeted_casts_are_not_dropped(self):
        series = [{
            "guid": 1297693, "name": "邪火轰炸",
            "events": [{
                "type": "cast", "timestamp": 10000, "targetID": None,
                "target": {"id": -1, "name": "Environment", "type": "NPC"},
            }],
        }]
        cast, = server.mplus_hostile_cast_events(series, {}, 0)
        self.assertIsNone(cast["who"])
        self.assertEqual(cast["name"], "邪火轰炸")

    def test_single_external_target_keeps_recipient(self):
        casts = [{"t": 10.0, "who": 1, "spell": 6940, "name": "牺牲祝福", "cat": "defensive"}]
        buffs = [{"t": 10.0, "who": 2, "source": 1, "spell": 6940,
                  "name": "牺牲祝福", "cat": "defensive"}]
        damage = [self.damage(10.3, who=2)]
        server.annotate_defensive_purposes(casts, buffs, damage, [], [], self.players, self.start)
        purpose, = casts[0]["purposes"]
        self.assertEqual(purpose["target"], "坦克")
        self.assertEqual(buffs[0]["purpose"], purpose)

    def test_group_buff_reports_one_purpose_without_a_single_target(self):
        casts = [{"t": 10.0, "who": 1, "spell": 97462, "name": "集结呐喊", "cat": "defensive"}]
        buffs = [{"t": 10.0, "who": who, "source": 1, "spell": 97462,
                  "name": "集结呐喊", "cat": "defensive"} for who in (1, 2)]
        server.annotate_defensive_purposes(casts, buffs, [self.damage(10.2)], [], [],
                                           self.players, self.start)
        purpose, = casts[0]["purposes"]
        self.assertNotIn("target", purpose)
        self.assertEqual([b["purpose"] for b in buffs], [purpose, purpose])
        self.assertEqual(server.defensive_targets(casts, buffs, self.players), [1])


class DefensiveEventFetchTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        cache_patch = patch.object(server, "CACHE", Path(self.cache.name))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_debuff_removals_are_filtered_by_target_name_and_type(self):
        first = {"timestamp": 150, "type": "removedebuff"}
        second = {"timestamp": 180, "type": "removedebuff", "targetID": 2}
        with patch.object(server.wcl, "get_json", side_effect=[
            {"events": [first], "nextPageTimestamp": 150},
            {"events": [second]},
        ]) as get_json:
            actual = server.mplus_debuff_events("report", 2, 100, 200, ["乙", "甲"])
        self.assertEqual(actual, [first, second])
        path = get_json.call_args_list[0].args[1]
        self.assertIn("/debuffs/", path)
        pins = parse_qs(urlparse(path).query)["pins"][0]
        self.assertIn("target.name IN (", pins)
        self.assertIn(json.dumps("甲", ensure_ascii=False), pins)
        self.assertIn(json.dumps("乙", ensure_ascii=False), pins)
        self.assertIn('type = "removedebuff"', pins)
        self.assertNotIn("target.id", pins)

    def test_damage_events_use_the_event_endpoint_and_player_pin(self):
        event = {"timestamp": 150, "type": "damage", "targetID": 1}
        with patch.object(server.wcl, "get_json", return_value={"events": [event]}) as get_json:
            self.assertEqual(server.mplus_damage_taken_events("report", 2, 100, 200, ["甲"]), [event])
        path = get_json.call_args.args[1]
        self.assertIn("/reports/events/damage-taken/report/2/100/200/", path)
        self.assertIn("/source/0/0/0/0/0/0/-1.0.-1.-1/0/Any/Any/2", path)
        pins = parse_qs(urlparse(path).query)["pins"][0]
        self.assertIn("target.name IN (" + json.dumps("甲", ensure_ascii=False) + ")", pins)

    def test_missing_names_do_not_fetch_every_player(self):
        with patch.object(server.wcl, "get_json") as get_json:
            self.assertEqual(server.mplus_debuff_events("report", 2, 100, 200, ["broken$pin"]), [])
            self.assertEqual(server.mplus_damage_taken_events("report", 2, 100, 200, []), [])
            get_json.assert_not_called()

    def test_damage_result_is_cached(self):
        with patch.object(server.wcl, "get_json", return_value={"events": []}) as get_json:
            server.mplus_damage_taken_events("report", 2, 100, 200, ["甲"])
            server.mplus_damage_taken_events("report", 2, 100, 200, ["甲"])
        self.assertEqual(get_json.call_count, 1)


if __name__ == "__main__":
    unittest.main()
