"""Licence check (POL-05): fail if a dependency's licence matches a denied pattern in policy/licenses.json.

    pip-licenses --format=json --with-urls > licenses-report.json
    python policy/tools/check_licenses.py licenses-report.json [--policy policy/licenses.json]

Input is the JSON report of `pip-licenses` (a list of {"Name", "Version", "License"}).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "licenses.json"


def check(report: list[dict], policy: dict) -> list[str]:
    denied = [p.lower() for p in policy.get("denied_patterns", [])]
    excepted = {e["package"].lower() for e in policy.get("exceptions", [])}
    problems = []
    for entry in report:
        name, license_text = entry.get("Name", "?"), str(entry.get("License", ""))
        if name.lower() in excepted:
            continue
        hit = next((p for p in denied if p in license_text.lower()), None)
        if hit:
            problems.append(f"ENT-050: {name} {entry.get('Version', '')} is licensed '{license_text}', which matches the denied pattern '{hit}'.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
    problems = check(report, policy)
    for problem in problems:
        print(problem)
    if not problems:
        print(f"Licences OK ({len(report)} packages).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
