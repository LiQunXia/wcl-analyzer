import json
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
