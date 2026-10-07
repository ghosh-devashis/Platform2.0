#!/usr/bin/env bash
# Run the policy bundle against an agent repo (POL-13, and the policy gate in CI).
#   policy/run-checks.sh [--fast] [target-dir]
# --fast  pre-commit subset (secrets, code rules, manifest, dependencies, waivers); a missing tool is skipped with a
#         warning. Without --fast (CI) every tool is required and a missing one fails the run.
set -u
FAST=0
if [ "${1:-}" = "--fast" ]; then FAST=1; shift; fi
TARGET="${1:-.}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAIL=0

step() {  # step <name> <tool> <command...>
  local name="$1" tool="$2"; shift 2
  if ! command -v "$tool" >/dev/null 2>&1; then
    if [ "$FAST" = 1 ]; then echo "SKIP  $name ($tool not installed)"; return; fi
    echo "FAIL  $name ($tool is required but not installed)"; FAIL=1; return
  fi
  if "$@"; then echo "PASS  $name"; else echo "FAIL  $name"; FAIL=1; fi
}

echo "Policy bundle $(cat "$HERE/VERSION") on $TARGET"
step "secret scan (ENT gitleaks)" gitleaks gitleaks detect --source "$TARGET" --config "$HERE/gitleaks.toml" --no-banner --redact
step "code rules (ENT-001..008)" semgrep semgrep scan --config "$HERE/semgrep" --error --quiet "$TARGET"
if [ -f "$TARGET/agent.yaml" ]; then
  step "manifest rules (ENT-020..023)" conftest conftest test --policy "$HERE/opa" --data "$HERE/data" --namespace manifest "$TARGET/agent.yaml"
  step "manifest schema" ent-agent ent-agent manifest validate "$TARGET/agent.yaml"
fi
if [ -f "$TARGET/pyproject.toml" ]; then
  step "dependency allowlist (ENT-010..012)" conftest conftest test --policy "$HERE/opa" --data "$HERE/data" --namespace dependencies "$TARGET/pyproject.toml"
fi
step "waivers (ENT-030..033)" python python "$HERE/tools/check_waivers.py" --root "$TARGET"
step "supported versions (ENT-060..062)" python python "$HERE/tools/check_versions.py" --root "$TARGET"
if [ "$FAST" = 0 ] && [ -d "$TARGET/.github/workflows" ]; then
  step "pipeline meta-check (ENT-040..043)" python python "$HERE/tools/check_pipeline.py" --root "$TARGET"
fi

if [ "$FAIL" = 0 ]; then echo "All policy checks passed."; else echo "Policy checks failed."; fi
exit "$FAIL"
