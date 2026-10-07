# Docker-socket mode (optional, off by default) (RUN-10)

## What it is

Floci can run some features only if it can start containers itself: its built-in web UI (`/_floci/ui`) and Lambda
function execution. That needs the **Docker socket** inside the Floci container.

## The risk

Anything that can talk to the Docker socket can start a privileged container, mount your disk and read or change anything
on the host. Giving it to Floci means a bug or vulnerability in Floci (or anything that reaches its API on port 4566) becomes
control of your machine. That is why the default stack does **not** mount it, and why the Floci UI runs as its own container
(`floci-ui` on port 4500) which needs no socket.

## When it is reasonable

- You are developing on your own machine and need Lambda execution or Floci's own UI.
- You turn it on for the session and turn it off afterwards.

Never use it in CI, on shared machines, or on a laptop with production credentials in the default AWS profile.

## How

```powershell
docker compose -f compose.yaml -f compose.docker-socket.yaml up -d floci     # on
docker compose up -d --force-recreate floci                                   # off again
```

Floci's data in S3 and DynamoDB is not persisted across a Floci restart (observed 2026-10-05), so recreate your test data afterwards
(`ent-agent dev seed` does this for an agent's manifest).

## Status

The override file is provided but has **not been exercised** (verifying it means mounting the socket, which the project
deliberately avoids). Treat it as a documented opt-in, and report what you find.
