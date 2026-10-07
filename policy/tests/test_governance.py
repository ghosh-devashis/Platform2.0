"""Tests for the governance tools: supported versions (GOV-02), compatibility matrix (GOV-01), register and dashboard (POL-11, GOV-03)."""

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "policy" / "tools"


def load(name, folder=TOOLS):
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


versions_tool = load("check_versions")
register_tool = load("build_register")
VERSIONS = json.loads((REPO / "policy" / "data" / "versions.json").read_text(encoding="utf-8"))
ENTRY = {"latest": "0.3.0", "warn_days": 90,
         "supported": [{"version": "0.3.0", "sunset": None}, {"version": "0.2.0", "sunset": "2027-01-10"}]}


def test_series():
    assert versions_tool.series("0.2.7") == "0.2" and versions_tool.series("v1.4") == "1.4" and versions_tool.series("x") is None


@pytest.mark.parametrize("version, today, rule", [
    ("0.3.5", date(2026, 10, 6), None),                 # latest series: fine
    ("0.2.1", date(2026, 10, 6), None),                 # previous series, sunset far away
    ("0.2.1", date(2026, 11, 20), "ENT-061"),           # within 90 days of the sunset: warn
    ("0.2.1", date(2027, 1, 11), "ENT-060"),            # past the sunset: fail
    ("0.1.0", date(2026, 10, 6), "ENT-060"),            # no longer supported at all
    (None, date(2026, 10, 6), "ENT-062"),
])
def test_assess(version, today, rule):
    result = versions_tool.assess("ent-agent-sdk", version, ENTRY, today)
    assert (result[0] if result else None) == rule


def test_versions_are_read_from_the_lock_file_pyproject_and_workflows(tmp_path):
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "ent-agent-sdk"\nversion = "0.2.4"\n[[package]]\nname = "other"\nversion = "9.9.9"\n', encoding="utf-8")
    assert versions_tool.sdk_version(tmp_path) == "0.2.4"
    (tmp_path / "uv.lock").unlink()
    (tmp_path / "pyproject.toml").write_text('[project]\nname="a"\ndependencies=["ent-agent-sdk~=0.1.2"]\n', encoding="utf-8")
    assert versions_tool.sdk_version(tmp_path) == "0.1.2"
    (tmp_path / "pyproject.toml").write_text('[project]\nname="a"\ndependencies=["ent-agent-sdk"]\n', encoding="utf-8")
    assert versions_tool.sdk_version(tmp_path) is None
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(yaml.safe_dump({"jobs": {"c": {"uses": "o/p/.github/workflows/agent-ci.yml@v0.3.1"}}}), encoding="utf-8")
    assert versions_tool.pipeline_version(tmp_path) == "0.3.1"


def test_cli_exit_codes(tmp_path, capsys):
    versions = tmp_path / "versions.json"
    versions.write_text(json.dumps({"deprecation": {"warn_days": 90}, "sdk": ENTRY, "policy_bundle": ENTRY}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text('[[package]]\nname = "ent-agent-sdk"\nversion = "0.2.0"\n', encoding="utf-8")
    assert versions_tool.main(["--root", str(repo), "--versions", str(versions), "--today", "2026-10-06"]) == 0
    assert versions_tool.main(["--root", str(repo), "--versions", str(versions), "--today", "2026-12-01"]) == 0  # warning only
    assert "ENT-061" in capsys.readouterr().out
    assert versions_tool.main(["--root", str(repo), "--versions", str(versions), "--today", "2027-02-01"]) == 1


def test_shipped_versions_file_matches_the_repository():
    sdk = __import__("tomllib").loads((REPO / "sdk" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    policy = (REPO / "policy" / "VERSION").read_text(encoding="utf-8").strip()
    assert VERSIONS["sdk"]["latest"] == sdk and VERSIONS["policy_bundle"]["latest"] == policy
    for key in ("sdk", "policy_bundle"):
        assert versions_tool.series(VERSIONS[key]["latest"]) in {versions_tool.series(s["version"]) for s in VERSIONS[key]["supported"]}
    # the platform's own workspace is on a supported release
    assert versions_tool.main(["--root", str(REPO)]) == 0


def test_compatibility_matrix_is_up_to_date():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "gen_compat_matrix.py"), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def make_repo(tmp_path, name, waivers, sdk="0.1.0", code=""):
    repo = tmp_path / name
    repo.mkdir()
    (repo / "agent.yaml").write_text(f"name: {name}\nteam: <b>sales</b>\ndata_classification: internal\n", encoding="utf-8")
    (repo / "waivers.yaml").write_text(yaml.safe_dump({"waivers": waivers}), encoding="utf-8")
    (repo / "uv.lock").write_text(f'[[package]]\nname = "ent-agent-sdk"\nversion = "{sdk}"\n', encoding="utf-8")
    (repo / "agent.py").write_text(code or "x = 1\n", encoding="utf-8")
    return repo


def test_register_and_dashboard(tmp_path):
    good = {"id": "W-1", "rule": "ENT-008", "reason": "<script>alert(1)</script>", "approver": "a@b.com", "expires": "2026-10-20"}
    old = {"id": "W-2", "rule": "ENT-009", "reason": "evaluation", "approver": "a@b.com", "expires": "2026-01-01"}
    repo_a = make_repo(tmp_path, "agent-a", [good, old], code="llm = ChatOpenAI()\n")
    repo_b = make_repo(tmp_path, "agent-b", [], sdk="0.0.9")
    out = tmp_path / "out"
    assert register_tool.main(["--repos", str(repo_a), str(repo_b), "--out", str(out), "--today", "2026-10-06"]) == 0

    register = json.loads((out / "register.json").read_text())
    assert register["totals"] == {"repos": 2, "policy_errors": 1, "waivers": 2, "expired_waivers": 1, "sdk_not_supported": 1}
    a = next(r for r in register["repos"] if r["repo"] == "agent-a")
    assert [w["status"] for w in a["waivers"]] == ["expiring soon", "expired"] and a["sdk_status"] == "supported"
    assert next(r for r in register["repos"] if r["repo"] == "agent-b")["sdk_status"] == "ENT-060"

    page = (out / "compliance.html").read_text(encoding="utf-8")
    assert "agent-a" in page and "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page  # escaped
    assert "&lt;b&gt;sales&lt;/b&gt;" in page and "expired" in page and "default-src 'none'" in page
    assert "agent-b" in (out / "register.md").read_text()


def test_register_needs_repositories(tmp_path, capsys):
    assert register_tool.main(["--out", str(tmp_path / "o")]) == 1