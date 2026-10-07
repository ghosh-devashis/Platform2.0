"""Central waiver register and compliance dashboard (POL-11, GOV-03).

    python policy/tools/build_register.py --repos path/to/repo-a path/to/repo-b [--out compliance] [--today YYYY-MM-DD]
    python policy/tools/build_register.py --repos-file repos.txt          # one path per line

For every agent repo it reads `agent.yaml`, `waivers.yaml`, the SDK version in `uv.lock`, the platform pipeline tag in
`.github/workflows`, and runs the in-process rule checker. Writes `<out>/register.json` (machine-readable, the central
register of every waiver in effect), `<out>/register.md` and `<out>/compliance.html` (a static dashboard: SDK
version status, policy findings, waivers expiring soon or expired). In CI, run it on a schedule over the checked-out
agent repos and publish the output.

Needs the SDK environment (PyYAML and `ent_agent_sdk`); `uv run python policy/tools/build_register.py ...`.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

TOOLS = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_waivers = _load("check_waivers")
check_versions = _load("check_versions")
EXPIRING_SOON_DAYS = 30


def repo_summary(repo: Path, versions: dict, today: date) -> dict:
    from ent_agent_sdk.devtools import lint

    manifest = {}
    manifest_path = repo / "agent.yaml"
    if manifest_path.is_file():
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            manifest = {}
    waivers, _ = check_waivers.load_waivers(repo)
    valid, problems = check_waivers.check_waivers(waivers, today)
    entries = []
    for waiver in waivers:
        if not isinstance(waiver, dict):
            continue
        try:
            expires = date.fromisoformat(str(waiver.get("expires")))
            status = "expired" if expires < today else ("expiring soon" if expires <= today + timedelta(days=EXPIRING_SOON_DAYS) else "active")
        except ValueError:
            status = "invalid"
        entries.append({**{k: waiver.get(k) for k in ("id", "rule", "reason", "approver", "expires")}, "status": status})
    sdk, pipeline = check_versions.sdk_version(repo), check_versions.pipeline_version(repo)
    warn = versions.get("deprecation", {}).get("warn_days", 90)
    sdk_problem = check_versions.assess("ent-agent-sdk", sdk, {**versions["sdk"], "warn_days": warn}, today)
    findings = lint.check_paths([repo])
    return {
        "repo": repo.name, "path": str(repo), "agent": manifest.get("name"), "team": manifest.get("team"),
        "data_classification": manifest.get("data_classification"),
        "sdk_version": sdk, "sdk_status": "supported" if sdk_problem is None else sdk_problem[0],
        "pipeline_version": pipeline,
        "policy_errors": sum(f.severity == "error" for f in findings), "policy_warnings": sum(f.severity == "warning" for f in findings),
        "waivers": entries, "waiver_problems": problems,
    }


def build(repos: list[Path], versions: dict, today: date) -> dict:
    summaries = [repo_summary(repo, versions, today) for repo in repos]
    return {"generated": today.isoformat(), "latest_sdk": versions["sdk"]["latest"], "repos": summaries,
            "totals": {"repos": len(summaries), "policy_errors": sum(s["policy_errors"] for s in summaries),
                       "waivers": sum(len(s["waivers"]) for s in summaries),
                       "expired_waivers": sum(w["status"] == "expired" for s in summaries for w in s["waivers"]),
                       "sdk_not_supported": sum(s["sdk_status"] == "ENT-060" for s in summaries)}}


def to_markdown(register: dict) -> str:
    lines = [f"# Compliance register ({register['generated']})", "",
             f"{register['totals']['repos']} repos, {register['totals']['waivers']} waivers "
             f"({register['totals']['expired_waivers']} expired), latest SDK {register['latest_sdk']}.", "",
             "| Repo | Team | Class. | SDK | Pipeline | Policy errors | Waivers |", "|---|---|---|---|---|---|---|"]
    for s in register["repos"]:
        lines.append(f"| {s['repo']} | {s['team'] or ''} | {s['data_classification'] or ''} | {s['sdk_version'] or '?'} ({s['sdk_status']}) | "
                     f"{s['pipeline_version'] or '?'} | {s['policy_errors']} | {len(s['waivers'])} |")
    waivers = [(s["repo"], w) for s in register["repos"] for w in s["waivers"]]
    if waivers:
        lines += ["", "## Waivers", "", "| Repo | ID | Rule | Approver | Expires | Status | Reason |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {r} | {w['id']} | {w['rule']} | {w['approver']} | {w['expires']} | {w['status']} | {w['reason']} |" for r, w in waivers]
    return "\n".join(lines) + "\n"


def to_html(register: dict) -> str:
    e = html.escape
    status_class = {"supported": "ok", "ENT-060": "bad", "ENT-061": "warn", "ENT-062": "warn"}

    def row(s: dict) -> str:
        sdk_class = status_class.get(s["sdk_status"], "warn")
        policy_class = "bad" if s["policy_errors"] else "ok"
        return (f"<tr><td>{e(s['repo'])}</td><td>{e(str(s['team'] or ''))}</td><td>{e(str(s['data_classification'] or ''))}</td>"
                f"<td class='{sdk_class}'>{e(str(s['sdk_version'] or '?'))} ({e(s['sdk_status'])})</td><td>{e(str(s['pipeline_version'] or '?'))}</td>"
                f"<td class='{policy_class}'>{s['policy_errors']} errors, {s['policy_warnings']} warnings</td><td>{len(s['waivers'])}</td></tr>")

    waiver_rows = "".join(
        f"<tr><td>{e(s['repo'])}</td><td>{e(str(w['id']))}</td><td>{e(str(w['rule']))}</td><td>{e(str(w['approver']))}</td>"
        f"<td>{e(str(w['expires']))}</td><td class='{'bad' if w['status'] in ('expired', 'invalid') else 'warn' if w['status'] == 'expiring soon' else 'ok'}'>{e(w['status'])}</td>"
        f"<td>{e(str(w['reason']))}</td></tr>" for s in register["repos"] for w in s["waivers"])
    t = register["totals"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Compliance dashboard</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>body{{font:14px system-ui,sans-serif;margin:2rem;color:#1f2328}}table{{border-collapse:collapse;margin:1rem 0}}
td,th{{border:1px solid #d0d7de;padding:.4rem .7rem;text-align:left}}th{{background:#f6f8fa}}.ok{{background:#dafbe1}}.warn{{background:#fff8c5}}.bad{{background:#ffebe9}}
.tiles{{display:flex;gap:1rem}}.tile{{border:1px solid #d0d7de;border-radius:6px;padding:.6rem 1rem}}.tile b{{font-size:1.6rem;display:block}}</style></head><body>
<h1>Compliance dashboard</h1><p>Generated {e(register['generated'])}. Latest SDK: {e(register['latest_sdk'])}.</p>
<div class="tiles"><div class="tile"><b>{t['repos']}</b>repos</div><div class="tile"><b>{t['policy_errors']}</b>policy errors</div>
<div class="tile"><b>{t['sdk_not_supported']}</b>unsupported SDK</div><div class="tile"><b>{t['waivers']}</b>waivers ({t['expired_waivers']} expired)</div></div>
<h2>Repositories</h2><table><tr><th>Repo</th><th>Team</th><th>Classification</th><th>SDK</th><th>Pipeline</th><th>Policy checks</th><th>Waivers</th></tr>
{''.join(row(s) for s in register['repos'])}</table>
<h2>Waivers</h2><table><tr><th>Repo</th><th>ID</th><th>Rule</th><th>Approver</th><th>Expires</th><th>Status</th><th>Reason</th></tr>{waiver_rows}</table>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", nargs="*", default=[])
    parser.add_argument("--repos-file")
    parser.add_argument("--out", default="compliance")
    parser.add_argument("--versions", default=str(TOOLS.parent / "data" / "versions.json"))
    parser.add_argument("--today", default=None)
    args = parser.parse_args(argv)
    paths = list(args.repos)
    if args.repos_file:
        paths += [line.strip() for line in Path(args.repos_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if not paths:
        print("No repositories given (--repos or --repos-file).")
        return 1
    today = date.fromisoformat(args.today) if args.today else date.today()
    register = build([Path(p) for p in paths], json.loads(Path(args.versions).read_text(encoding="utf-8")), today)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "register.json").write_text(json.dumps(register, indent=2), encoding="utf-8")
    (out / "register.md").write_text(to_markdown(register), encoding="utf-8")
    (out / "compliance.html").write_text(to_html(register), encoding="utf-8")
    totals = register["totals"]
    print(f"Wrote {out}/register.json, register.md, compliance.html "
          f"({totals['repos']} repos, {totals['waivers']} waivers, {totals['expired_waivers']} expired, {totals['policy_errors']} policy errors).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
