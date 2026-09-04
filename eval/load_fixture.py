#!/usr/bin/env python3
"""Загрузка eval-фикстуры в локальную SQLite-базу."""
import argparse
from pathlib import Path

from eval import goldenset, levels
from storage import db

LEVEL_FIXTURE = levels.ROOT / "eval-fixture-4-levels.json"
DEFAULT_FIXTURE = levels.ROOT / "eval-fixture.json"


def fixture_path(value=""):
    if value:
        return Path(value)
    if LEVEL_FIXTURE.exists():
        return LEVEL_FIXTURE
    return DEFAULT_FIXTURE


def load(path):
    meta, rows = goldenset.load(path, "fixture")
    repo = db.Repository()
    try:
        applied = repo.apply(rows)
    finally:
        repo.close()
    return meta, applied


def main():
    ap = argparse.ArgumentParser(description="Загрузить eval-фикстуру в SQLite")
    ap.add_argument("--fixture", default="",
                    help="файл фикстуры; по умолчанию eval-fixture-4-levels.json")
    args = ap.parse_args()

    path = fixture_path(args.fixture)
    if not path.exists():
        print("нет файла фикстуры %s" % path)
        return
    try:
        meta, applied = load(path)
    except goldenset.SetVersionError as bad:
        print(bad)
        return
    print("база %s" % db.path())
    print("набор версии %d, подпись факта: %s" % (meta["version"], meta.get("identity")))
    print("применено мутаций: %d" % applied)


if __name__ == "__main__":
    main()
