import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mdt_export  # noqa: E402


class MdtCodecTest(unittest.TestCase):
    def test_cbor_round_trip(self):
        value = {
            "text": "测试路线",
            "value": {
                "currentDungeonIdx": 160,
                "pulls": [
                    {1: [1, 2], 5: [1]},
                    {2: [1]},
                ],
                "teeming": False,
            },
            "empty": [],
        }
        encoded = mdt_export.cbor_encode(value)
        self.assertEqual(mdt_export._cbor_decode(encoded, [0])[0], value)

    def test_strings_are_byte_strings(self):
        encoded = mdt_export.cbor_encode("abc")
        self.assertEqual(encoded[0] >> 5, 2)  # CBOR byte string, not a text string

    def test_mdt_string_round_trip(self):
        preset = {"text": "WCL 测试 +20", "value": {"currentDungeonIdx": 160, "pulls": [{2: [1]}]}}
        encoded = mdt_export.encode_mdt2(preset)
        self.assertTrue(encoded.startswith("!~MDT2~"))
        raw = zlib.decompress(
            __import__("base64").b64decode(encoded[len(mdt_export.PREFIX):]), -15
        )
        self.assertTrue(raw)
        self.assertEqual(mdt_export.decode_mdt2(encoded), preset)


class BundledDataTest(unittest.TestCase):
    def test_bundled_catalog_has_numeric_enemy_and_clone_indices(self):
        catalog = mdt_export.load_dungeons(Path(__file__).resolve().parent.parent / "mdt_dungeons.json")
        self.assertTrue(catalog)
        self.assertTrue(all(isinstance(index, int) for index in catalog[0]["enemies"]))
        enemy = next(iter(catalog[0]["enemies"].values()))
        self.assertTrue(all(isinstance(index, int) for index in enemy["clones"]))


class BuildRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_path = root / "mdt.json"
        self.data_path.write_text('''[
          {"index":999,"key":"TestDungeon","names":["Test Dungeon","测试副本"],"enemies":{
            "1":{"id":1001,"count":60,"clones":{"1":{"x":1,"y":-1,"g":1,"sublevel":1}}},
            "2":{"id":1002,"count":20,"clones":{"1":{"x":3,"y":-2,"g":2,"sublevel":1}}},
            "3":{"id":1003,"count":0,"isBoss":true,"clones":{"1":{"x":4,"y":-3,"g":3,"sublevel":1}}}
          }}
        ]''', "utf-8")
        self.run = {"name": "测试副本", "level": 20, "countRequired": 100}
        self.pulls = [
            {"forcesPct": 60, "enemies": [{"guid": 1001, "engaged": 1, "killed": 1}]},
            {"forcesPct": 20, "enemies": [{"guid": 1002, "engaged": 1, "killed": 1}]},
            {"forcesPct": 0, "boss": 1, "enemies": [{"guid": 1003, "engaged": 1, "killed": 1, "boss": True}]},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_route_is_valid_and_importable(self):
        result = mdt_export.build_route(self.data_path, self.run, self.pulls, "TESTCODE", 1)
        self.assertTrue(result["string"].startswith("!~MDT2~"))
        self.assertEqual(result["dungeon_index"], 999)
        self.assertEqual(result["pulls"], 3)
        decoded = mdt_export.decode_mdt2(result["string"])
        self.assertEqual(decoded["value"]["currentDungeonIdx"], 999)
        self.assertEqual(decoded["text"], "WCL 测试副本 +20")
        # Each pull references valid MDT enemy/clone indices, without reusing a clone.
        enemies = mdt_export.load_dungeons(self.data_path)[0]["enemies"]
        seen = set()
        for pull in decoded["value"]["pulls"]:
            # A pull whose only key is enemy index 1 is a dense Lua sequence; CBOR encodes it
            # as an array, which deserializes back to the exact same Lua table.
            if isinstance(pull, list):
                pull = {i + 1: value for i, value in enumerate(pull)}
            for enemy_idx, clones in pull.items():
                self.assertIn(enemy_idx, enemies)
                for clone_idx in clones:
                    self.assertIn(clone_idx, enemies[enemy_idx]["clones"])
                    self.assertNotIn((enemy_idx, clone_idx), seen)
                    seen.add((enemy_idx, clone_idx))
        self.assertEqual(result["force"], 80)

    def test_export_contains_native_notes_and_schematic_arrow(self):
        data = self.data_path.read_text("utf-8").replace('"x":3', '"x":30')
        self.data_path.write_text(data, "utf-8")
        pulls = [{"id": 10, "forcesPct": 80, "enemies": self.pulls[0]["enemies"] + self.pulls[1]["enemies"]}]
        route = {"pulls": [{"id": 10, "start": 0, "steps": [
            {"kind": "join", "label": "A", "t": 0, "members": [{"guid": 1001, "name": "甲"}]},
            {"kind": "stop", "t": 8, "until": 20, "x": 999999, "y": 999999},
            {"kind": "join", "label": "B", "t": 30, "chain": True, "alive": 1,
             "members": [{"guid": 1002, "name": "乙"}]},
        ]}]}
        result = mdt_export.build_route(self.data_path, self.run, pulls, tank_route=route)
        objects = mdt_export.decode_mdt2(result["string"])["objects"]
        self.assertEqual(result["notes"], 3)
        self.assertEqual(result["arrows"], 1)
        arrow = next(o for o in objects if o.get("t"))
        self.assertEqual(arrow["l"], [1.0, -1.0, 30.0, -2.0])
        self.assertEqual(arrow["d"][2:4], [1, True])
        text = "\n".join(o["d"][4] for o in objects if o.get("n"))
        self.assertIn("示意位置", text)
        self.assertIn("停留约 12s", text)
        self.assertIn("旧怪仍存活", text)
        self.assertNotIn("999999", str(objects))  # WCL coordinates never leak into MDT space.

    def test_unmatched_analysis_does_not_invent_an_annotation_anchor(self):
        result = mdt_export.build_route(self.data_path, self.run, self.pulls,
                                       tank_route={"pulls": [{"id": 999, "steps": [{"kind": "join"}]}]})
        self.assertEqual(result["notes"], 0)
        self.assertEqual(result["arrows"], 0)


if __name__ == "__main__":
    unittest.main()
