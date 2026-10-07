"""get_model() with no name takes the model from agent.yaml, so agent code never repeats it."""

from pathlib import Path

import pytest

from ent_agent_sdk.models import ModelConfigError, declared_models, get_model

HEADER = "name: probe-agent\nteam: learning\ndata_classification: internal\nguardrail_profile: standard\n"


def manifest(tmp_path: Path, models_block: str) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(HEADER + models_block, encoding="utf-8")
    return path


def test_no_name_uses_the_first_model_in_agent_yaml(tmp_path):
    path = manifest(tmp_path, "models:\n  - chat-fast\n  - chat-default\n")
    assert get_model(manifest=path).model_name == "chat-fast"


def test_changing_agent_yaml_changes_the_model_without_touching_code(tmp_path):
    path = manifest(tmp_path, "models: [chat-default]\n")
    assert get_model(manifest=path).model_name == "chat-default"
    manifest(tmp_path, "models: [chat-fast]\n")
    assert get_model(manifest=path).model_name == "chat-fast"


def test_the_manifest_is_found_through_agent_manifest_or_the_working_directory(tmp_path, monkeypatch):
    path = manifest(tmp_path, "models: [chat-fast]\n")
    monkeypatch.setenv("AGENT_MANIFEST", str(path))
    assert get_model().model_name == "chat-fast"
    monkeypatch.delenv("AGENT_MANIFEST")
    monkeypatch.chdir(tmp_path)  # agent.yaml in the current folder
    assert get_model().model_name == "chat-fast"


def test_an_explicit_name_wins_and_other_settings_still_apply(tmp_path):
    path = manifest(tmp_path, "models: [chat-default]\n")
    model = get_model("chat-fast", manifest=path, temperature=0)
    assert model.model_name == "chat-fast" and model.temperature == 0


def test_declared_models_keeps_the_order_of_agent_yaml(tmp_path):
    path = manifest(tmp_path, "models:\n  - chat-fast\n  - chat-default\n")
    assert declared_models(path) == ("chat-fast", "chat-default")


def test_a_missing_manifest_gives_a_clear_way_out(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_MANIFEST", raising=False)
    monkeypatch.chdir(tmp_path)  # no agent.yaml here
    with pytest.raises(ModelConfigError) as caught:
        get_model()
    message = str(caught.value)
    assert "was not found" in message and "AGENT_MANIFEST" in message and 'get_model("chat-default")' in message


def test_a_manifest_without_models_is_refused(tmp_path):
    path = manifest(tmp_path, "models: []\n")
    with pytest.raises(ModelConfigError, match="lists no models"):
        get_model(manifest=path)


def test_an_invalid_manifest_is_reported_not_hidden(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text("name: Not A Valid Name\n", encoding="utf-8")
    with pytest.raises(ModelConfigError, match="Cannot read the model"):
        get_model(manifest=path)


def test_an_empty_name_is_still_rejected():
    with pytest.raises(ModelConfigError, match="must not be empty"):
        get_model("")
