"""Отбор eval-случаев по уровню сложности."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / "eval-cases.json"
LEVEL_CASES = ROOT / "eval-cases-4-levels.json"


def parse_difficulty(value):
    """`1,3` и `1-4` в упорядоченный набор уровней."""
    if value is None or value == "":
        return ()
    out = set()
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise argparse.ArgumentTypeError("сложность должна быть числом") from exc
            if start > end:
                raise argparse.ArgumentTypeError("диапазон сложности задан наоборот")
            out.update(range(start, end + 1))
        else:
            try:
                out.add(int(part))
            except ValueError as exc:
                raise argparse.ArgumentTypeError("сложность должна быть числом") from exc
    bad = sorted(n for n in out if n < 1 or n > 4)
    if bad:
        raise argparse.ArgumentTypeError("известны уровни сложности 1..4")
    return tuple(sorted(out))


def cases_path(cases, difficulty):
    """Без явного файла `--difficulty` выбирает новый synthetic-набор."""
    if cases:
        return Path(cases)
    return LEVEL_CASES if difficulty else DEFAULT_CASES


def filter_cases(cases, only="", kind="", difficulty=()):
    """Общий порядок отбора для evaluate и matrix."""
    out = list(cases)
    if only:
        out = [c for c in out if only in c["id"]]
    if kind:
        out = [c for c in out if c.get("kind") == kind]
    if difficulty:
        want = set(difficulty)
        out = [c for c in out if c.get("difficulty") in want]
    return out
