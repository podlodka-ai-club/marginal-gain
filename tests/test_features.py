#!/usr/bin/env python3
"""Признаки модуля оценки в боевом коде. Запуск: python3 -m unittest tests.test_features -v

Глоссарий описывает 35 признаков, а посчитаны они только в research/lab/x7/feat.py —
в исследовательском коде. Стенд меряет собственную копию формулы, боевой код про
неё не знает. Отсюда требование этих проверок: формула живёт в боевом модуле,
стенд считает ею же, и число замера относится к продукту, а не к копии.

Первый признак — n_log, частота, сглаженная логарифмом. В score_of та же величина
уже участвует, но в виде repeat, с потолком на десяти вхождениях.
"""
import math, sys, unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "research" / "lab" / "x7"))

from domain import features
from pipeline import understand


class Formula(unittest.TestCase):
    def test_log1p(self):
        self.assertEqual(features.n_log({"n": 0}), 0.0)
        self.assertAlmostEqual(features.n_log({"n": 1}), math.log(2))
        self.assertAlmostEqual(features.n_log({"n": 30}), math.log(31))

    def test_no_ceiling(self):
        """Отличие от repeat: потолка нет, тридцать вхождений весят больше десяти."""
        self.assertGreater(features.n_log({"n": 30}), features.n_log({"n": 10}))

    def test_registry(self):
        self.assertIn("n_log", features.NAMES)
        self.assertEqual(features.compute({"n": 4}), {"n_log": features.n_log({"n": 4})})


class Wiring(unittest.TestCase):
    def test_understand_exposes_feature(self):
        rec = {"n": 3, "projects": {"a"}, "last": "2026-08-20T10:00:00Z"}
        self.assertEqual(understand.features_of(rec)["n_log"], math.log(4))

    def test_score_of_untouched(self):
        """Признак считается рядом. В меру ADR 0002 не входит: слой решения отдельно."""
        rec = {"n": 3, "projects": {"a"}, "last": ""}
        self.assertEqual(understand.score_of(rec, ""),
                         round(0.5 * 0.3 + 0.2 * (1 / 3.0), 3))


class Stand(unittest.TestCase):
    def test_stand_calls_production_function(self):
        """Подменяем боевую функцию — столбец стенда обязан поехать за ней.

        Проверка импорта тут не годится: стенд может импортировать модуль
        и всё равно считать столбец своей копией формулы.
        """
        import feat
        with mock.patch.object(features, "n_log", lambda rec: 42.0):
            keys, X, agg = feat.build("pkey", "2026-08-15")
        j = feat.NAMES.index("n_log")
        self.assertTrue((X[:, j] == 42.0).all())

    def test_stand_column_is_the_formula(self):
        import feat
        keys, X, agg = feat.build("pkey", "2026-08-15")
        j = feat.NAMES.index("n_log")
        for i, k in enumerate(keys):
            self.assertAlmostEqual(X[i, j], math.log1p(agg[k]["n"]))


class Holdout(unittest.TestCase):
    def test_auc_matches_glossary(self):
        """Отложенное окно: признаки строго до 15.08, метка — всплыл ли узел до 26.08."""
        import feat
        keys, X, agg = feat.build("pkey", "2026-08-15")
        y = feat.labels("pkey", keys, "2026-08-15", "2026-08-26")
        s = np.array([features.n_log({"n": agg[k]["n"]}) for k in keys])
        self.assertEqual(len(y), 386)
        self.assertEqual(int(y.sum()), 29)
        self.assertAlmostEqual(feat.auc(s, y), 0.662, places=3)


if __name__ == "__main__":
    unittest.main()
