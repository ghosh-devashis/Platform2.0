"""The `ent-agent` command line.

    ent-agent new <name> --team <team> [--classification internal] [--dir .]   create an agent project
    ent-agent manifest validate [agent.yaml]                                    check a manifest
    ent-agent dev seed [--manifest agent.yaml] [--image IMAGE]                  seed the local Floci stack
    ent-agent metadata                                                           print SDK/policy build metadata
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ent_agent_sdk import manifest as manifest_mod
from ent_agent_sdk import metadata
from ent_agent_sdk.devtools import lint, scaffold, seed


def evals_error() -> type[Exception]:
    from ent_agent_sdk.devtools.evals import EvalError

    return EvalError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ent-agent", description="Enterprise Agent SDK tools")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create an agent project from the standard template")
    new.add_argument("name", help="agent name: lowercase letters, digits, hyphens")
    new.add_argument("--team", required=True, help="owning team")
    new.add_argument("--classification", default="internal", choices=manifest_mod.CLASSIFICATIONS)
    new.add_argument("--dir", default=".", help="parent directory (default: current directory)")

    manifest = sub.add_parser("manifest", help="manifest tools")
    manifest_sub = manifest.add_subparsers(dest="manifest_command", required=True)
    validate = manifest_sub.add_parser("validate", help="validate an agent.yaml")
    validate.add_argument("path", nargs="?", default="agent.yaml")

    dev = sub.add_parser("dev", help="local development helpers")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    seed_parser = dev_sub.add_parser("seed", help="create the manifest's secrets and AgentCore runtime in Floci")
    seed_parser.add_argument("--manifest", default="agent.yaml")
    seed_parser.add_argument("--image", help="container image URI to register (default local/<name>:dev)")

    update = sub.add_parser("update", help="pull template changes into a project created with `ent-agent new`")
    update.add_argument("path", nargs="?", default=".")
    update.add_argument("--apply", action="store_true", help="write the safe changes (default: show a dry run with diffs)")
    update.add_argument("--check", action="store_true", help="exit 1 if the project is behind the template")

    check = sub.add_parser("check", help="check code, agent.yaml and pyproject.toml against the ENT policy rules")
    check.add_argument("paths", nargs="*", default=["."], help="files or directories (default: current directory)")
    check.add_argument("--json", action="store_true", help="machine-readable output (used by the VS Code extension)")

    evaluation = sub.add_parser("eval", help="evaluation gate: golden questions with minimum scores")
    evaluation_sub = evaluation.add_subparsers(dest="eval_command", required=True)
    run_eval = evaluation_sub.add_parser("run", help="run a golden file against a running agent")
    run_eval.add_argument("golden", nargs="?", default="evals/golden.yaml")
    run_eval.add_argument("--url", default="http://localhost:8080", help="agent base URL")
    run_eval.add_argument("--report", help="write the full JSON report here")
    run_eval.add_argument("--min", action="append", default=[], metavar="DIMENSION=SCORE", help="override a minimum score")

    sub.add_parser("metadata", help="print SDK and policy build metadata")
    sub.add_parser("mcp", help="run the MCP standards server on stdio (for AI assistants and editors)")
    rules = sub.add_parser("rules", help="list the policy rules")
    rules.add_argument("rule_id", nargs="?", help="explain one rule, e.g. ENT-001")
    rules.add_argument("--markdown", action="store_true", help="print the full rules page (policy/docs/rules.md)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "new":
            files = scaffold.create_project(args.name, team=args.team, classification=args.classification,
                                            directory=args.dir)
            print(f"Created {args.name} ({len(files)} files). Next:")
            print(f"  cd {args.dir}/{args.name} && uv sync && uv run pytest")
            print("  See README.md for running it against the local stack.")
        elif args.command == "manifest":
            loaded = manifest_mod.load(args.path)
            print(f"{args.path} is valid: {loaded.name} ({loaded.team}, {loaded.data_classification}, "
                  f"guardrails: {loaded.guardrail_profile}).")
        elif args.command == "dev":
            result = seed.seed(manifest_mod.load(args.manifest), image=args.image)
            print(f"Secrets created: {', '.join(result['secrets_created']) or 'none'}; "
                  f"kept: {', '.join(result['secrets_kept']) or 'none'}.")
            if result["resources_created"] or result["resources_kept"]:
                print(f"Resources created: {', '.join(result['resources_created']) or 'none'}; kept: {', '.join(result['resources_kept']) or 'none'}.")
            runtime = result["runtime"]
            print(f"AgentCore runtime: {runtime['name']} ({runtime['arn']}).")
        elif args.command == "update":
            plan = scaffold.update_project(args.path, apply=args.apply)
            print(f"Template version {plan.from_version} -> {plan.to_version}")
            for title, items in (("Updated", plan.updated), ("Added", plan.added), ("Kept (you changed them)", plan.kept)):
                if items:
                    print(f"{title}: {', '.join(items)}")
            if plan.conflicts:
                where = "new versions saved as <file>.template-new" if args.apply else "run with --apply to save the new versions"
                print(f"CONFLICTS (changed by you and by the template; {where}): {', '.join(plan.conflicts)}")
            if not args.apply:
                for relative, diff in plan.diffs.items():
                    print(f"\n--- {relative}\n{diff}", end="")
                if plan.needs_attention:
                    print("\nDry run: nothing was changed. Re-run with --apply to update.")
            if not plan.needs_attention:
                print("Already up to date.")
            return 1 if (args.check and plan.needs_attention) else 0
        elif args.command == "check":
            findings = lint.check_paths(args.paths)
            if args.json:
                print(lint.to_json(findings))
            else:
                for f in findings:
                    print(f"{f.path}:{f.line}:{f.column}: {f.severity}: {f.message}")
                print(f"{len(findings)} finding(s)." if findings else "No findings.")
            return 1 if any(f.severity == "error" for f in findings) else 0
        elif args.command == "eval":
            from ent_agent_sdk.devtools import evals

            overrides = {}
            for item in args.min:
                name, _, value = item.partition("=")
                overrides[name] = float(value)
            data = evals.load(args.golden)
            report = evals.run(data, evals.http_invoke(args.url), overrides)
            print(evals.format_report(report))
            if args.report:
                Path(args.report).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
            return 0 if report.passed else 1
        elif args.command == "mcp":
            from ent_agent_sdk.devtools import mcp_server

            mcp_server.serve()
        elif args.command == "rules":
            from ent_agent_sdk.devtools import rules_catalog

            if args.markdown:
                sys.stdout.write(rules_catalog.render_markdown())
            elif args.rule_id:
                rule = rules_catalog.get(args.rule_id)
                if rule is None:
                    print(f"error: unknown rule '{args.rule_id}'", file=sys.stderr)
                    return 1
                print(f"{rule['id']}: {rule['title']}\n\nWhy:  {rule['why']}\nFix:  {rule['fix']}\n\nbad:\n{rule['bad']}\n\ngood:\n{rule['good']}")
            else:
                for rule in rules_catalog.RULES:
                    print(f"{rule['id']}  {rule['title']}")
        elif args.command == "metadata":
            print(json.dumps(metadata.read(), indent=2))
    except (manifest_mod.ManifestError, scaffold.ScaffoldError, seed.SeedError, evals_error()) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
