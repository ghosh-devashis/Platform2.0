# __NAME__: standards for AI assistants and developers

This agent is built on the Enterprise Agent SDK (`ent-agent-sdk`). CI rejects code that breaks these rules, so
follow them when writing or changing code.

## Always
- **Start the app with `EnterpriseAgentApp`** (usually `EnterpriseAgentApp.from_manifest(graph, "agent.yaml")`). Never
  create your own FastAPI/uvicorn server.
- **Get the model with `get_model()`** from `ent_agent_sdk.models`, with no name: it uses the first model listed under
  `models:` in `agent.yaml`, so the choice lives in the manifest and is never hardcoded in code. Names come from the
  approved registry (`chat-default`, `chat-fast`). Only an agent that really uses several models passes a name
  (`get_model("chat-fast")`), and that name must also be listed under `models:`.
- **Register tools with `@tools.tool(owner=..., data_classification=..., side_effects=...)`** from
  `ent_agent_sdk`, and bind them with `tools.bind(model, [...])`. List them under `tools:` in `agent.yaml`. Set
  `side_effects=True` for anything that changes state (it is audited and never retried automatically).
- **Read secrets with `secrets.get("name")`** (or `aget` in async code). List them under `secrets:` in `agent.yaml`.
- **Use `ent_agent_sdk.identity`** for outbound API credentials (short-lived tokens).
- Keep `agent.yaml` accurate: owner team, data classification (restricted data needs `guardrail_profile: strict`).
- Write tests with a scripted fake model (see `tests/test_agent.py`). Tests must not need keys or network.

## Never
- Construct `ChatOpenAI`, `ChatAnthropic`, `ChatBedrock` or any provider client, or call provider URLs directly (all
  model traffic goes through the gateway via `get_model`). Do not use the `openai`, `anthropic` or `bedrock-runtime`
  clients.
- Read credentials from environment variables, files or code (`os.environ["..._KEY"]`), or call Secrets Manager with
  raw `boto3`.
- Add `langchain*`, `langgraph`, `openai`, `anthropic` or `boto3` to `pyproject.toml`: the SDK pins the approved
  versions. Other dependencies must be on the approved list.
- Log or return prompts, responses, secrets or personal data. Do not disable guardrails; if you must, use
  `Guardrails.off(waiver="<id>")` with an approved entry in `waivers.yaml`.
- Commit `.env` files, keys or tokens.

## Commands
- `uv sync`: install. `uv run pytest`: tests. `uv run ent-agent manifest validate`: check `agent.yaml`.
- `docker compose --profile app up -d --build`: run against the local platform stack (see README.md).
- Policy checks before committing: `$PLATFORM_DIR/policy/run-checks.sh --fast` (also runs via pre-commit).
- `uv run ent-agent check` checks code, `agent.yaml` and `pyproject.toml` against the rules in milliseconds (the VS Code extension shows the same findings live).
- This repo configures the MCP standards server (`ent-agent mcp`): ask it for `get_standards` and `get_example`, and run `check_code` on code you write before presenting it.
- Tools with `side_effects=True` pause for human approval; `uv run ent-agent eval run evals/golden.yaml` runs the evaluation gate CI enforces.

## When CI fails
Each failure names a rule ID (ENT-0xx) with the reason and the fix. Fix the code; request a waiver only for a
genuine, time-boxed exception.
