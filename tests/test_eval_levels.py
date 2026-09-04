import json, os, tempfile, unittest
from collections import Counter
from pathlib import Path

from eval import goldenset, levels, load_fixture
from storage import db

HERE = Path(__file__).resolve().parent.parent


class TestDifficultySpec(unittest.TestCase):
    def test_single_level(self):
        self.assertEqual(levels.parse_difficulty("4"), (4,))

    def test_list_of_levels(self):
        self.assertEqual(levels.parse_difficulty("1,3"), (1, 3))

    def test_range_of_levels(self):
        self.assertEqual(levels.parse_difficulty("1-4"), (1, 2, 3, 4))

    def test_rejects_unknown_levels(self):
        with self.assertRaises(Exception):
            levels.parse_difficulty("5")

    def test_difficulty_switch_uses_the_four_level_set_by_default(self):
        self.assertEqual(levels.cases_path("", (4,)), levels.LEVEL_CASES)
        self.assertEqual(levels.cases_path("", ()), levels.DEFAULT_CASES)

    def test_filter_can_combine_only_kind_and_difficulty(self):
        cases = [
            {"id": "a-1", "kind": "fact", "difficulty": 1},
            {"id": "a-2", "kind": "fact", "difficulty": 2},
            {"id": "b-2", "kind": "context", "difficulty": 2},
        ]
        got = levels.filter_cases(cases, only="a", kind="fact", difficulty=(2,))
        self.assertEqual([c["id"] for c in got], ["a-2"])


class TestFourLevelEvalSet(unittest.TestCase):
    def test_shipped_cases_have_twenty_cases_per_level(self):
        _, cases = goldenset.load(HERE / "eval-cases-4-levels.json", "cases")
        self.assertEqual(Counter(c.get("difficulty") for c in cases),
                         {1: 20, 2: 20, 3: 20, 4: 20})

    def test_four_level_fixture_can_answer_non_negative_cases(self):
        _, cases = goldenset.load(HERE / "eval-cases-4-levels.json", "cases")
        _, rows = goldenset.load(HERE / "eval-fixture-4-levels.json", "fixture")
        blob = json.dumps(rows, ensure_ascii=False)
        blind = [c["id"] for c in cases if c.get("expect")
                 and not all(want in blob for want in c["expect"])]
        self.assertEqual(blind, [])


class TestLoadFixture(unittest.TestCase):
    def test_load_fixture_writes_to_the_selected_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("XMEM_LOCAL_PATH")
            os.environ["XMEM_LOCAL_PATH"] = str(Path(tmp) / "memory.db")
            try:
                meta, applied = load_fixture.load(HERE / "eval-fixture-4-levels.json")
                repo = db.Repository()
                try:
                    counts = repo.counts()
                finally:
                    repo.close()
            finally:
                if old is None:
                    os.environ.pop("XMEM_LOCAL_PATH", None)
                else:
                    os.environ["XMEM_LOCAL_PATH"] = old
        self.assertEqual(meta["identity"], "synthetic-eval-v1")
        self.assertEqual(applied, 150)
        self.assertEqual(counts["Fact"], 150)


if __name__ == "__main__":
    unittest.main()
