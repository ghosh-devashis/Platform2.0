"""Pipeline meta-check (POL-10): the repo's CI must call the platform's reusable pipeline and not weaken it.

    python policy/tools/check_pipeline.py [--root DIR]

Rules (exit code 1 if any fail):
- ENT-040  .github/workflows must contain a job that calls .../.github/workflows/agent-ci.yml@<ref>
- ENT-041  the ref must be a release tag or commit SHA, not a branch (main, master, HEAD)
- ENT-042  that job must not be conditional (`if:`) or allowed to fail (`continue-on-error`)
- ENT-043  the workflow must run on pull requests

Needs PyYAML (installed with ent-agent-sdk).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

CALL = re.compile(r"/\.github/workflows/agent-ci\.yml@(?P<ref>\S+)$")
BRANCHES = {"main", "master", "HEAD", "develop"}


def check_workflow(name: str, workflow: dict) -> tuple[bool, list[str]]:
    """(calls the platform pipeline?, problems) for one parsed workflow file."""
    problems: list[str] = []
    calls = False
    # YAML 1.1 reads the key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    for job_id, job in (workflow.get("jobs") or {}).items():
        match = CALL.search(str(job.get("uses", "")))
        if not match:
            continue
        calls = True
        if match.group("ref") in BRANCHES:
            problems.append(f"ENT-041: {name} job '{job_id}' pins the platform pipeline to branch '{match.group('ref')}'. Use a release tag or commit SHA.")
        if "if" in job:
            problems.append(f"ENT-042: {name} job '{job_id}' is conditional; the platform pipeline must always run.")
        if job.get("continue-on-error") in (True, "true"):
            problems.append(f"ENT-042: {name} job '{job_id}' is allowed to fail; remove continue-on-error.")
        has_pr = (
            triggers == "pull_request"
            or (isinstance(triggers, list) and "pull_request" in triggers)
            or (isinstance(triggers, dict) and "pull_request" in triggers)
        )
        if not has_pr:
            problems.append(f"ENT-043: {name} does not run on pull_request.")
    return calls, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    workflows = sorted((Path(args.root) / ".github" / "workflows").glob("*.y*ml"))
    problems: list[str] = []
    found = False
    for path in workflows:
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            problems.append(f"ENT-040: {path.name} is not valid YAML.")
            continue
        calls, issues = check_workflow(path.name, workflow)
        found = found or calls
        problems += issues
    if not found:
        problems.append("ENT-040: no workflow calls the platform pipeline (.../.github/workflows/agent-ci.yml@<tag>).")
    for problem in problems:
        print(problem)
    if not problems:
        print("Pipeline OK.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
