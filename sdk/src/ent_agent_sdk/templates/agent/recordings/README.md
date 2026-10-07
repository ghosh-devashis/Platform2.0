# Recorded model answers

CI answers every model call from the recordings in this folder (replay mode: no provider keys, cost or internet),
and fails if a request has no recording. Commit the recordings with your tests.

To record real answers while developing (needs real provider keys in the local Floci secrets):

```bash
ROUTER_MODE=record ENT_RECORDINGS_DIR=$PWD/recordings docker compose --profile app up -d --build
# exercise the agent: run your integration tests or call /invocations
git add recordings && git commit
```

Recordings hold model responses only (not requests), keyed by model, messages, tools and tool choice.
Review them before committing: they can contain whatever the model said.
