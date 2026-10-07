"""Project scaffolding (TPL-01) and template updates (TPL-03).

`ent-agent new <name>` creates a ready-to-run agent repo from the standard template; `ent-agent update` later pulls
template changes into it as a reviewable diff.

Template files live in `ent_agent_sdk/templates/agent/`. Placeholders (`__NAME__`, `__PACKAGE__`, `__TEAM__`,
`__CLASSIFICATION__`, `__PROFILE__`) are replaced in file contents and in file and folder names. Path parts that
start with `dot_` become dotfiles (`dot_github` -> `.github`), so the template ships without hidden files.

Updates: the project remembers what it was generated from in `.ent-template.json` (template version and a hash of
every generated file). `update_project` re-renders today's template and, per file:
- you haven't changed it and the template did -> updated (with `--apply`);
- you changed it and the template didn't     -> left alone;
- you changed it and the template did too    -> conflict: your file is kept, the new version is written next to it
                                                as `<file>.template-new` for you to merge by hand;
- the template added a file                  -> added. Files you deleted on purpose stay deleted.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from ent_agent_sdk import manifest as manifest_mod

TEMPLATE_VERSION = "2"
STATE_FILE = ".ent-template.json"


class ScaffoldError(RuntimeError):
    pass


def _replacements(name: str, team: str, classification: str) -> dict[str, str]:
    return {
        "__NAME__": name,
        "__PACKAGE__": name.replace("-", "_"),
        "__TEAM__": team.strip(),
        "__CLASSIFICATION__": classification,
        "__PROFILE__": "strict" if classification == "restricted" else "standard",
    }


def _output_name(name: str, replacements: dict[str, str]) -> str:
    for token, value in replacements.items():
        name = name.replace(token, value)
    return "." + name[len("dot_"):] if name.startswith("dot_") else name


def _walk(source: Traversable, replacements: dict[str, str], prefix: str = "") -> Iterator[tuple[str, str]]:
    for entry in sorted(source.iterdir(), key=lambda e: e.name):
        if entry.name == "__pycache__":
            continue
        relative = f"{prefix}{_output_name(entry.name, replacements)}"
        if entry.is_dir():
            yield from _walk(entry, replacements, relative + "/")
            continue
        text = entry.read_text(encoding="utf-8")
        for token, value in replacements.items():
            text = text.replace(token, value)
        yield relative, text


def render_template(name: str, team: str, classification: str = "internal") -> dict[str, str]:
    """The template's files for these parameters, as {relative path: text}."""
    return dict(_walk(files("ent_agent_sdk") / "templates" / "agent", _replacements(name, team, classification)))


def _validated(name: str, team: str, classification: str) -> None:
    profile = "strict" if classification == "restricted" else "standard"
    problems = manifest_mod.validate(
        {"name": name, "team": team, "data_classification": classification, "guardrail_profile": profile}
    )
    if problems:
        raise ScaffoldError("; ".join(problems))


def _hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _state(rendered: dict[str, str], name: str, team: str, classification: str) -> str:
    return json.dumps(
        {"template_version": TEMPLATE_VERSION, "name": name, "team": team, "classification": classification,
         "files": {path: _hash(text) for path, text in sorted(rendered.items())}},
        indent=2,
    ) + "\n"


def create_project(name: str, *, team: str, classification: str = "internal", directory: str | Path = ".") -> list[Path]:
    """Create `<directory>/<name>` from the template. Returns the files written."""
    _validated(name, team, classification)
    target = Path(directory) / name
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(f"'{target}' already exists and is not empty.")
    rendered = render_template(name, team, classification)
    written: list[Path] = []
    for relative, text in rendered.items():
        _write(target / relative, text)
        written.append(target / relative)
    _write(target / STATE_FILE, _state(rendered, name, team, classification))
    return [*written, target / STATE_FILE]


@dataclass
class UpdatePlan:
    from_version: str
    to_version: str
    updated: list[str] = field(default_factory=list)    # untouched by you, changed in the template
    added: list[str] = field(default_factory=list)      # new in the template
    conflicts: list[str] = field(default_factory=list)  # changed on both sides
    kept: list[str] = field(default_factory=list)       # changed by you only
    unchanged: list[str] = field(default_factory=list)
    diffs: dict[str, str] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return bool(self.updated or self.added or self.conflicts or self.from_version != self.to_version)


def update_project(path: str | Path = ".", *, apply: bool = False) -> UpdatePlan:
    """Compare a generated project with today's template; with `apply=True`, pull the safe changes in."""
    root = Path(path)
    state_path = root / STATE_FILE
    if not state_path.is_file():
        raise ScaffoldError(f"'{root}' has no {STATE_FILE}: it wasn't created with `ent-agent new`, so it can't be updated.")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    try:  # the manifest is the source of truth for the parameters (you may have changed the team or classification)
        current = manifest_mod.load(root / "agent.yaml")
        name, team, classification = current.name, current.team, current.data_classification
    except manifest_mod.ManifestError:
        name, team, classification = state["name"], state["team"], state["classification"]
    rendered = render_template(name, team, classification)
    base: dict[str, str] = state["files"]
    plan = UpdatePlan(from_version=str(state.get("template_version", "0")), to_version=TEMPLATE_VERSION)
    new_base = dict(base)

    for relative, new_text in rendered.items():
        target = root / relative
        new_hash = _hash(new_text)
        if relative not in base:
            plan.added.append(relative)
            if apply and not target.exists():
                _write(target, new_text)
                new_base[relative] = new_hash
            elif target.exists():  # you already have a file there: treat as yours
                plan.added.remove(relative)
                plan.kept.append(relative)
            continue
        if not target.exists():  # deleted on purpose
            continue
        user_text = target.read_text(encoding="utf-8")
        if _hash(user_text) == new_hash:  # already what the template now produces (e.g. you changed the team yourself)
            plan.unchanged.append(relative)
            new_base[relative] = new_hash
            continue
        user_changed, template_changed = _hash(user_text) != base[relative], new_hash != base[relative]
        if not template_changed:
            (plan.kept if user_changed else plan.unchanged).append(relative)
        elif not user_changed:
            plan.updated.append(relative)
            plan.diffs[relative] = "".join(difflib.unified_diff(
                user_text.splitlines(True), new_text.splitlines(True), f"a/{relative}", f"b/{relative}"))
            if apply:
                _write(target, new_text)
                new_base[relative] = new_hash
        else:
            plan.conflicts.append(relative)
            plan.diffs[relative] = "".join(difflib.unified_diff(
                user_text.splitlines(True), new_text.splitlines(True), f"yours/{relative}", f"template/{relative}"))
            if apply:
                _write(root / f"{relative}.template-new", new_text)

    if apply:
        new_state = json.loads(_state({}, name, team, classification))
        new_state["files"] = dict(sorted(new_base.items()))
        # a conflict stays "changed on both sides" until you resolve it, so keep its old base hash
        state_path.write_text(json.dumps(new_state, indent=2) + "\n", encoding="utf-8")
    return plan
