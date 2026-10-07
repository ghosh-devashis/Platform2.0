"""Deprecation check (GOV-02): fail when a repo is on an SDK or policy-bundle release past its sunset date.

    python policy/tools/check_versions.py [--root DIR] [--versions policy/data/versions.json] [--today YYYY-MM-DD]

Reads the repo's resolved `ent-agent-sdk` version (uv.lock, else the pyproject pin) and the platform pipeline tag it
calls (.github/workflows/*.yml, `agent-ci.yml@vX.Y.Z`), and compares them with policy/data/versions.json.

- ENT-060  the release is not supported, or its sunset date has passed (error: fails the build)
- ENT-061  the release is supported but sunsets within `warn_days` (warning: the build passes, with a reminder)
- ENT-062  the version could not be determined (warning)

Needs PyYAML (installed with ent-agent-sdk).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

import yaml

DEFAULT_VERSIONS = Path(__file__).resolve().parents[1] / "data" / "versions.json"
CALL = re.compile(r"/\.github/workflows/agent-ci\.yml@v?(?P<ref>\S+)$")


def series(version: str) -> str | None:
    """'0.1.3' -> '0.1' (the support window is per minor series)."""
    match = re.match(r"v?(\d+)\.(\d+)", version.strip())
    return f"{match.group(1)}.{match.group(2)}" if match else None


def sdk_version(root: Path) -> str | None:
    lock = root / "uv.lock"
    if lock.is_file():
        try:
            for package in tomllib.loads(lock.read_text(encoding="utf-8")).get("package", []):
                if package.get("name") == "ent-agent-sdk" and package.get("version"):
                    return package["version"]
        except tomllib.TOMLDecodeError:
            pass
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            for dep in tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("dependencies", []):
                match = re.match(r"ent[-_]agent[-_]sdk\s*(?:[=~><!]=?|===)\s*([0-9][0-9.]*)", dep)
                if match:
                    return match.group(1)
        except tomllib.TOMLDecodeError:
            pass
    return None


def pipeline_version(root: Path) -> str | None:
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")) if (root / ".github" / "workflows").is_dir() else []:
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for job in (workflow.get("jobs") or {}).values():
            match = CALL.search(str(job.get("uses", "")))
            if match:
                return match.group("ref")
    return None


def assess(label: str, version: str | None, entry: dict, today: date) -> tuple[str, str] | None:
    """(rule, message) for a release that needs attention, else None."""
    if version is None:
        return "ENT-062", f"ENT-062: could not determine the {label} version this repo uses."
    wanted = series(version)
    supported = {series(s["version"]): s for s in entry["supported"]}
    if wanted is None or wanted not in supported:
        latest = entry["latest"]
        return "ENT-060", f"ENT-060: {label} {version} is not a supported release. Upgrade to {latest} (supported: {', '.join(sorted(s for s in supported if s))})."
    sunset = supported[wanted].get("sunset")
    if sunset:
        sunset_date = date.fromisoformat(sunset)
        if sunset_date < today:
            return "ENT-060", f"ENT-060: {label} {version} reached its sunset on {sunset}. Upgrade to {entry['latest']}."
        days = (sunset_date - today).days
        if days <= entry.get("warn_days", 90):
            return "ENT-061", f"ENT-061: {label} {version} sunsets on {sunset} ({days} days). Plan the upgrade to {entry['latest']}."
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    parser.add_argument("--today", default=None)
    args = parser.parse_args(argv)
    root, today = Path(args.root), date.fromisoformat(args.today) if args.today else date.today()
    data = json.loads(Path(args.versions).read_text(encoding="utf-8"))
    warn_days = data.get("deprecation", {}).get("warn_days", 90)
    failed = False
    checks = [("ent-agent-sdk", sdk_version(root), {**data["sdk"], "warn_days": warn_days}),
              ("platform policy bundle", pipeline_version(root), {**data["policy_bundle"], "warn_days": warn_days})]
    for label, version, entry in checks:
        if label == "platform policy bundle" and version is None and not (root / ".github" / "workflows").is_dir():
            continue  # no pipeline file at all: ENT-040 (check_pipeline) reports that
        problem = assess(label, version, entry, today)
        if problem:
            print(problem[1])
            failed = failed or problem[0] == "ENT-060"
        else:
            print(f"{label} {version}: supported.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
