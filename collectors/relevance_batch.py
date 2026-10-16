"""Offline batch evaluator for locally saved collector JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Mapping

from collectors.relevance import DIRECT, IRRELEVANT, POTENTIAL, RULESET_VERSION, classify


def _publications(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in ("publications", "cases"):
            rows = value.get(key)
            if isinstance(rows, list):
                return rows
        if "external_id" in value:
            return [value]
    raise ValueError("input must be a publication list or collector JSON with publications/cases")


def evaluate(value: object) -> dict[str, object]:
    publications = _publications(value)
    results = [classify(publication).to_dict() for publication in publications]
    counts = Counter(result["relevance"] for result in results)
    return {
        "ruleset_version": RULESET_VERSION,
        "counts": {label: counts.get(label, 0) for label in (DIRECT, POTENTIAL, IRRELEVANT)},
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify locally saved collector JSON; does not access the network or alter input files.")
    parser.add_argument("input", type=Path, help="UTF-8 JSON file produced by a collector")
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding="utf-8") as file:
            value = json.load(file)
        result = evaluate(value)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"relevance batch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
