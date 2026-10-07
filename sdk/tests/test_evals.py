"""Tests for the evaluation gate (POL-08)."""

import http.server
import json
import threading
from pathlib import Path

import pytest

from ent_agent_sdk.cli import main
from ent_agent_sdk.devtools import evals

REPO = Path(__file__).resolve().parents[2]


def golden(**extra):
    data = {"cases": [
        {"id": "greet", "prompt": "hi", "checks": [
            {"type": "contains", "value": "hello", "ignore_case": True},
            {"type": "not_contains", "value": "secret", "dimension": "safety"},
            {"type": "status", "value": 200},
        ]},
        {"id": "math", "prompt": "2+2", "checks": [
            {"type": "regex", "value": r"\b4\b", "dimension": "groundedness"},
            {"type": "equals", "value": "four"},
        ]},
    ]}
    data.update(extra)
    return data


def scripted(answers):
    def invoke(prompt, session_id):
        assert len(session_id) >= 20
        return answers[prompt]
    return invoke


def test_scores_per_dimension_and_pass():
    report = evals.run(golden(), scripted({"hi": (200, "HELLO there"), "2+2": (200, "the answer is 4, four")}),
                       thresholds={"task_success": 0.5})
    # task_success: contains + status + equals(fails) = 2/3; safety 1/1; groundedness 1/1
    assert report.scores == {"task_success": 0.6667, "safety": 1.0, "groundedness": 1.0}
    assert report.passed and report.failed_dimensions == []


def test_a_dimension_below_its_minimum_fails_the_gate():
    report = evals.run(golden(thresholds={"task_success": 0.9}), scripted({"hi": (200, "hello"), "2+2": (200, "4")}))
    assert report.failed_dimensions == ["task_success"] and not report.passed
    text = evals.format_report(report)
    assert "FAILED" in text and "math" in text and "expected the answer to equal 'four'" in text


def test_safety_defaults_to_a_perfect_score():
    report = evals.run(golden(), scripted({"hi": (200, "hello, the secret is 42"), "2+2": (200, "4 four")}),
                       thresholds={"task_success": 0.1})
    assert report.failed_dimensions == ["safety"]


def test_unreachable_agent_fails_cases_instead_of_crashing():
    def down(prompt, session_id):
        raise ConnectionError("refused")

    report = evals.run(golden(), down)
    assert not report.passed and report.cases[0].status == 0


@pytest.mark.parametrize("check, text, status, expected", [
    ({"type": "contains", "value": "A"}, "abc", 200, False),
    ({"type": "contains", "value": "A", "ignore_case": True}, "abc", 200, True),
    ({"type": "not_contains", "value": "x"}, "abc", 200, True),
    ({"type": "equals", "value": "abc"}, " abc ", 200, True),
    ({"type": "regex", "value": "^a.c$"}, "abc", 200, True),
    ({"type": "not_regex", "value": "\\d"}, "abc1", 200, False),
    ({"type": "status", "value": 502}, "", 502, True),
])
def test_check_types(check, text, status, expected):
    assert evals._run_check(check, status, text).passed is expected


@pytest.mark.parametrize("data, message", [
    ({}, "non-empty 'cases'"),
    ({"cases": [{"prompt": "x", "checks": []}]}, "at least one check"),
    ({"cases": [{"checks": [{"type": "contains", "value": "a"}]}]}, "'prompt' is required"),
    ({"cases": [{"prompt": "x", "checks": [{"type": "magic", "value": 1}]}]}, "'type'"),
    ({"cases": [{"prompt": "x", "checks": [{"type": "regex", "value": "(", "dimension": "safety"}]}]}, "regular expression"),
    ({"cases": [{"prompt": "x", "checks": [{"type": "contains", "value": "a", "dimension": "vibes"}]}]}, "dimension"),
    ({"thresholds": {"vibes": 1}, "cases": [{"prompt": "x", "checks": [{"type": "contains", "value": "a"}]}]}, "thresholds.vibes"),
])
def test_invalid_golden_files_are_rejected(data, message):
    with pytest.raises(evals.EvalError, match=message):
        evals.validate(data)


class _Agent(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        prompt = json.loads(self.rfile.read(int(self.headers["content-length"])))["prompt"]
        assert len(self.headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]) >= 33
        self.send_response(200 if prompt != "boom" else 500)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"output": f"Hello from the stub: {prompt}"} if prompt != "boom" else {"error": {"type": "AgentError"}}).encode())

    def log_message(self, *args):
        pass


def test_http_invoke_and_cli_gate(tmp_path, capsys):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert evals.http_invoke(url)("hi", "s") == (200, "Hello from the stub: hi")
        status, text = evals.http_invoke(url)("boom", "s")
        assert status == 500 and "AgentError" in text

        good = tmp_path / "good.yaml"
        good.write_text("cases:\n  - id: a\n    prompt: hi\n    checks:\n      - {type: contains, value: hello, ignore_case: true}\n"
                        "      - {type: not_contains, value: password, dimension: safety}\n", encoding="utf-8")
        report_file = tmp_path / "report.json"
        assert main(["eval", "run", str(good), "--url", url, "--report", str(report_file)]) == 0
        assert json.loads(report_file.read_text())["passed"] is True
        assert "PASSED" in capsys.readouterr().out

        bad = tmp_path / "bad.yaml"
        bad.write_text("cases:\n  - id: a\n    prompt: hi\n    checks:\n      - {type: contains, value: goodbye}\n", encoding="utf-8")
        assert main(["eval", "run", str(bad), "--url", url]) == 1
        assert main(["eval", "run", str(bad), "--url", url, "--min", "task_success=0"]) == 0  # threshold override
        assert main(["eval", "run", str(tmp_path / "missing.yaml"), "--url", url]) == 1
    finally:
        server.shutdown()


def test_the_templates_golden_file_is_valid():
    path = REPO / "sdk" / "src" / "ent_agent_sdk" / "templates" / "agent" / "evals" / "golden.yaml"
    assert evals.load(path)["cases"], path
