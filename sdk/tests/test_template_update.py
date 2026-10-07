"""Tests for template updates (TPL-03) and the template's MCP/eval files."""

import json
from pathlib import Path

import pytest

from ent_agent_sdk.cli import main
from ent_agent_sdk.devtools import scaffold


@pytest.fixture
def project(tmp_path):
    scaffold.create_project("crm-agent", team="sales", classification="internal", directory=tmp_path)
    return tmp_path / "crm-agent"


def newer_template(monkeypatch, changes):
    """Pretend the platform team changed some template files (and added one)."""
    real = scaffold.render_template

    def render(name, team, classification="internal"):
        files = real(name, team, classification)
        files.update({path: text for path, text in changes.items() if path in files or text is not None})
        return files

    monkeypatch.setattr(scaffold, "render_template", render)


def test_generated_project_remembers_what_it_was_generated_from(project):
    state = json.loads((project / ".ent-template.json").read_text())
    assert state["template_version"] == scaffold.TEMPLATE_VERSION and state["name"] == "crm-agent"
    assert "CLAUDE.md" in state["files"] and ".ent-template.json" not in state["files"]


def test_template_ships_mcp_config_and_golden_questions(project):
    assert json.loads((project / ".mcp.json").read_text())["mcpServers"]["ent-agent-standards"]["args"] == ["run", "ent-agent", "mcp"]
    assert json.loads((project / ".vscode" / "mcp.json").read_text())["servers"]["ent-agent-standards"]["type"] == "stdio"
    assert "cases:" in (project / "evals" / "golden.yaml").read_text() and "crm-agent" in (project / "evals" / "golden.yaml").read_text()


def test_up_to_date_project_has_nothing_to_do(project):
    plan = scaffold.update_project(project)
    assert not plan.needs_attention and plan.updated == [] and plan.conflicts == []


def test_untouched_files_update_your_edits_survive_and_conflicts_get_a_side_file(project, monkeypatch):
    (project / "CLAUDE.md").write_text((project / "CLAUDE.md").read_text() + "\nOur team rule: be kind.\n", encoding="utf-8")  # you edit
    (project / "AGENTS.md").write_text("# our own agents file\n", encoding="utf-8")                                              # you edit
    newer_template(monkeypatch, {
        "waivers.yaml": "# new header from the platform team\nwaivers: []\n",        # you didn't touch it -> updated
        "AGENTS.md": "# platform agents file v2\n",                                  # both changed -> conflict
        "pyproject.toml": (project / "pyproject.toml").read_text(),                  # template unchanged -> nothing
        "NEW_FILE.md": "added by the template\n",                                    # new file
    })

    dry = scaffold.update_project(project)
    assert dry.updated == ["waivers.yaml"] and dry.conflicts == ["AGENTS.md"] and dry.added == ["NEW_FILE.md"]
    assert "CLAUDE.md" in dry.kept
    assert "+# new header from the platform team" in dry.diffs["waivers.yaml"]
    assert "new header" not in (project / "waivers.yaml").read_text()  # a dry run changes nothing
    assert not (project / "NEW_FILE.md").exists()

    applied = scaffold.update_project(project, apply=True)
    assert applied.updated == ["waivers.yaml"]
    assert (project / "waivers.yaml").read_text().startswith("# new header")
    assert (project / "NEW_FILE.md").read_text() == "added by the template\n"
    assert (project / "AGENTS.md").read_text() == "# our own agents file\n"                  # yours is untouched
    assert (project / "AGENTS.md.template-new").read_text() == "# platform agents file v2\n"  # theirs, beside it
    assert "Our team rule: be kind." in (project / "CLAUDE.md").read_text()

    again = scaffold.update_project(project)  # waivers.yaml and NEW_FILE.md are now in step; the conflict remains
    assert again.updated == [] and again.added == [] and again.conflicts == ["AGENTS.md"]


def test_files_you_deleted_stay_deleted(project, monkeypatch):
    (project / "waivers.yaml").unlink()
    newer_template(monkeypatch, {"waivers.yaml": "changed\n"})
    plan = scaffold.update_project(project, apply=True)
    assert not (project / "waivers.yaml").exists() and "waivers.yaml" not in plan.updated


def test_line_endings_alone_do_not_count_as_changes(project):
    text = (project / "waivers.yaml").read_text()
    (project / "waivers.yaml").write_bytes(text.replace("\n", "\r\n").encode())  # e.g. git autocrlf on Windows
    assert scaffold.update_project(project).kept == []


def test_the_manifest_decides_the_parameters(project):
    manifest_path = project / "agent.yaml"
    manifest_path.write_text(manifest_path.read_text().replace("team: sales", "team: payments"), encoding="utf-8")
    plan = scaffold.update_project(project)
    assert "agent.yaml" in plan.unchanged and plan.conflicts == []  # your edit already matches the new parameters
    assert "README.md" in plan.updated and "payments" in plan.diffs["README.md"]  # files that mention the team follow it
    scaffold.update_project(project, apply=True)
    assert "payments" in (project / "README.md").read_text() and not scaffold.update_project(project).needs_attention


def test_not_a_generated_project(tmp_path):
    with pytest.raises(scaffold.ScaffoldError, match="ent-agent new"):
        scaffold.update_project(tmp_path)


def test_cli_update_modes(project, monkeypatch, capsys):
    assert main(["update", str(project)]) == 0
    assert "Already up to date" in capsys.readouterr().out
    newer_template(monkeypatch, {"waivers.yaml": "# v2\nwaivers: []\n"})
    assert main(["update", str(project), "--check"]) == 1
    assert "Dry run" in capsys.readouterr().out
    assert main(["update", str(project), "--apply"]) == 0
    assert (project / "waivers.yaml").read_text().startswith("# v2")
    assert main(["update", str(project.parent / "nowhere")]) == 1
