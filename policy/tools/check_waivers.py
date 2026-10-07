"""Waiver check (POL-11): exceptions must be approved, time-boxed and actually referenced.

    python policy/tools/check_waivers.py [--root DIR] [--today YYYY-MM-DD]

Rules (exit code 1 if any fail):
- ENT-030  every waiver in waivers.yaml has id, rule, reason, approver and a valid expires date
- ENT-031  expired waivers fail the build
- ENT-032  `Guardrails.off(waiver="<id>")` must name an unexpired ENT-008 waiver and `approval_waiver="<id>"` an unexpired ENT-009 waiver
- ENT-033  inline `# nosem` suppressions are not allowed: use waivers.yaml

Needs PyYAML (installed with ent-agent-sdk).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

REQUIRED = ("id", "rule", "reason", "approver", "expires")
SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__", "templates", "policy"}
GUARDRAILS_OFF = re.compile(r"""Guardrails\.off\(\s*waiver\s*=\s*["']([^"']+)["']""")
APPROVAL_OFF = re.compile(r"""approval_waiver\s*=\s*["']([^"']+)["']""")
NOSEM = re.compile(r"#\s*nosem\b")


def load_waivers(root: Path) -> tuple[list[dict], list[str]]:
    path = root / "waivers.yaml"
    if not path.is_file():
        return [], []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return [], ["ENT-030: waivers.yaml is not valid YAML."]
    waivers = data.get("waivers") or []
    if not isinstance(waivers, list):
        return [], ["ENT-030: 'waivers' in waivers.yaml must be a list."]
    return waivers, []


def check_waivers(waivers: list[dict], today: date) -> tuple[dict[str, dict], list[str]]:
    """Validate waivers. Returns the valid, unexpired ones by ID and the problems found."""
    problems: list[str] = []
    valid: dict[str, dict] = {}
    for index, waiver in enumerate(waivers, start=1):
        label = waiver.get("id", f"#{index}") if isinstance(waiver, dict) else f"#{index}"
        if not isinstance(waiver, dict):
            problems.append(f"ENT-030: waiver {label} must be a mapping.")
            continue
        missing = [f for f in REQUIRED if not str(waiver.get(f, "")).strip()]
        if missing:
            problems.append(f"ENT-030: waiver {label} is missing {', '.join(missing)} (approver and expiry are mandatory).")
            continue
        expires = waiver["expires"]
        try:
            expiry = expires if isinstance(expires, date) else date.fromisoformat(str(expires))
        except ValueError:
            problems.append(f"ENT-030: waiver {label} has an invalid expires date '{expires}' (use YYYY-MM-DD).")
            continue
        if expiry < today:
            problems.append(f"ENT-031: waiver {label} for {waiver['rule']} expired on {expiry}. Fix the code or renew the waiver.")
            continue
        valid[str(waiver["id"])] = waiver
    return valid, problems


def scan_code(root: Path, valid: dict[str, dict]) -> list[str]:
    problems: list[str] = []
    for path in root.rglob("*.py"):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for waiver_id in GUARDRAILS_OFF.findall(text):
            waiver = valid.get(waiver_id)
            if waiver is None or waiver["rule"] != "ENT-008":
                problems.append(
                    f"ENT-032: {path.relative_to(root)} disables guardrails with waiver '{waiver_id}', which is not an "
                    "approved, unexpired ENT-008 waiver in waivers.yaml."
                )
        for waiver_id in APPROVAL_OFF.findall(text):
            waiver = valid.get(waiver_id)
            if waiver is None or waiver["rule"] != "ENT-009":
                problems.append(
                    f"ENT-032: {path.relative_to(root)} switches off tool approval with waiver '{waiver_id}', which is not an "
                    "approved, unexpired ENT-009 waiver in waivers.yaml."
                )
        if NOSEM.search(text):
            problems.append(f"ENT-033: {path.relative_to(root)} uses an inline `nosem` suppression. Use waivers.yaml instead.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--today", default=None, help="override today's date (for tests)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    today = date.fromisoformat(args.today) if args.today else date.today()

    waivers, problems = load_waivers(root)
    valid, more = check_waivers(waivers, today)
    problems += more + scan_code(root, valid)
    for problem in problems:
        print(problem)
    if not problems:
        print(f"Waivers OK ({len(valid)} active).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
