"""Tests for the policy tools: waivers (POL-11), pipeline meta-check (POL-10), licences (POL-05)."""

import importlib.util
from datetime import date
from pathlib import Path

import pytest
import yaml

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


waivers_tool = load("check_waivers")
pipeline_tool = load("check_pipeline")
licenses_tool = load("check_licenses")

TODAY = date(2026, 10, 5)
GOOD = {"id": "W-1", "rule": "ENT-008", "reason": "evaluation", "approver": "a@example.com", "expires": "2026-12-31"}


def test_valid_waiver_is_active():
    valid, problems = waivers_tool.check_waivers([GOOD], TODAY)
    assert problems == [] and "W-1" in valid


@pytest.mark.parametrize("change, code", [
    ({"approver": ""}, "ENT-030"),
    ({"expires": None}, "ENT-030"),
    ({"expires": "soon"}, "ENT-030"),
    ({"expires": "2026-01-01"}, "ENT-031"),
])
def test_bad_waivers_are_rejected(change, code):
    _, problems = waivers_tool.check_waivers([{**GOOD, **change}], TODAY)
    assert problems and problems[0].startswith(code)


def write_project(tmp_path, code, waivers):
    (tmp_path / "agent.py").write_text(code, encoding="utf-8")
    (tmp_path / "waivers.yaml").write_text(yaml.safe_dump({"waivers": waivers}), encoding="utf-8")
    return tmp_path


def test_guardrails_off_needs_an_approved_unexpired_waiver(tmp_path):
    code = 'g = Guardrails.off(waiver="W-1")\n'
    assert waivers_tool.main(["--root", str(write_project(tmp_path, code, [GOOD])), "--today", "2026-10-05"]) == 0
    assert waivers_tool.main(["--root", str(write_project(tmp_path, code, [])), "--today", "2026-10-05"]) == 1
    assert waivers_tool.main(["--root", str(write_project(tmp_path, code, [GOOD])), "--today", "2027-02-01"]) == 1
    other_rule = {**GOOD, "rule": "ENT-001"}
    assert waivers_tool.main(["--root", str(write_project(tmp_path, code, [other_rule])), "--today", "2026-10-05"]) == 1


def test_inline_nosem_is_rejected(tmp_path):
    root = write_project(tmp_path, "x = 1  # nosem: ent-001\n", [])
    assert waivers_tool.main(["--root", str(root), "--today", "2026-10-05"]) == 1


def workflow(job):
    return {"on": ["push", "pull_request"], "jobs": {"agent-ci": job}}


def test_pipeline_meta_check():
    uses = "org/platform2.0/.github/workflows/agent-ci.yml@v0.1.0"
    calls, problems = pipeline_tool.check_workflow("ci.yml", workflow({"uses": uses}))
    assert calls and problems == []
    _, problems = pipeline_tool.check_workflow("ci.yml", workflow({"uses": uses.replace("v0.1.0", "main")}))
    assert problems[0].startswith("ENT-041")
    _, problems = pipeline_tool.check_workflow("ci.yml", workflow({"uses": uses, "if": "github.ref == 'x'"}))
    assert problems[0].startswith("ENT-042")
    _, problems = pipeline_tool.check_workflow("ci.yml", workflow({"uses": uses, "continue-on-error": True}))
    assert problems[0].startswith("ENT-042")
    calls, _ = pipeline_tool.check_workflow("ci.yml", workflow({"runs-on": "ubuntu-latest"}))
    assert not calls


def test_pipeline_requires_a_workflow_file_calling_the_platform(tmp_path):
    assert pipeline_tool.main(["--root", str(tmp_path)]) == 1
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    text = yaml.safe_dump({"on": {"pull_request": None},
                           "jobs": {"ci": {"uses": "o/p/.github/workflows/agent-ci.yml@" + "a" * 40}}})
    (wf / "ci.yml").write_text(text, encoding="utf-8")
    assert pipeline_tool.main(["--root", str(tmp_path)]) == 0


def test_template_ci_passes_its_own_meta_check(tmp_path):
    template = Path(__file__).resolve().parents[2] / "sdk/src/ent_agent_sdk/templates/agent/dot_github/workflows/ci.yml"
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    assert pipeline_tool.main(["--root", str(tmp_path)]) == 0


def test_licence_check():
    policy = {"denied_patterns": ["AGPL", "GNU General Public License"], "exceptions": [{"package": "okpkg"}]}
    report = [{"Name": "a", "Version": "1", "License": "MIT License"},
              {"Name": "b", "Version": "2", "License": "GNU General Public License v3"},
              {"Name": "okpkg", "Version": "3", "License": "AGPL-3.0"}]
    problems = licenses_tool.check(report, policy)
    assert len(problems) == 1 and "b 2" in problems[0] and problems[0].startswith("ENT-050")
