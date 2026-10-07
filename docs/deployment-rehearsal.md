# Deployment rehearsal (RUN-05)

## What it is
Deploying an agent to AWS means running scripts that create a runtime, an IAM role, secrets and so on. If the
script is wrong you normally find out in a real AWS account. **Deployment rehearsal** lets you run the *same*
Terraform against **Floci** (the local AWS look-alike) first, with no AWS account and no cost.

Terraform is the standard "infrastructure as code" tool: you describe the resources you want in `.tf` files and it
creates, changes or deletes them to match. The code lives in `deploy/terraform/`; `deploy/deploy.ps1` is a wrapper
that reads the agent's `agent.yaml`, runs Terraform, then checks that the result works ("smoke checks").

## How to run a local rehearsal
Prerequisite: the local stack is up (`docker compose up -d`; Floci on :4566, invoke router on :4567).

```powershell
. .\harness\env.ps1
.\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent          # plan only
.\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent -Apply   # create + checks
.\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent -Destroy # clean up
```
`-NameOverride` makes the runtime `rehearsal_agent`, so it cannot collide with your agent's own runtime.
The first run downloads the AWS Terraform provider (large, about 800 MB unpacked); later runs reuse the cache.

What you get: Secrets Manager secrets (placeholder values), an IAM role, optional S3 bucket / DynamoDB table, and
an AgentCore runtime, tagged with team / data classification / environment. Smoke checks then verify the secrets,
that the runtime is `READY`, and that the invoke router lists it. Any failure gives a non-zero exit code.

## One Terraform, two targets
`-Target local` sets `target = "local"`: the AWS provider's endpoints (S3, DynamoDB, IAM, STS, Secrets Manager,
`bedrockagentcore`) point at `http://localhost:4566`, dummy keys are used and account/credential checks are
skipped. `-Target aws` uses your normal AWS credential chain and refuses to apply or destroy unless you pass
`-ConfirmRealAws`. The resources themselves do not change between targets. The only conditional difference besides
the provider settings: secrets are deleted immediately locally (real AWS keeps them 30 days, which reserves the
name).

## What differs from real AWS (honest limits)
- **Nothing runs.** Floci's AgentCore is control-plane only: the runtime is a *record*. The container image is
  never pulled or started (the image URI `local/<name>:dev` is just a string). To actually call the agent locally
  use the existing harness and invoke router.
- **Real AWS needs more:** an image pushed to ECR, valid credentials, and an IAM role that really trusts
  `bedrock-agentcore.amazonaws.com`. Floci does not enforce IAM, so a wrong policy or trust relationship will
  **not** be caught by a rehearsal. Quotas, regional availability and eventual-consistency delays are not simulated
  either; runtime status goes straight to `READY`.
- **ARNs and ids differ** (Floci uses account `000000000000` and its own id format).
- Only the services used here are rehearsed. Things added later (endpoints, memory, gateway, VPC networking, JWT
  authorizers) must be checked against Floci's support before relying on a rehearsal.
- Floci does not keep S3 and DynamoDB data across a Floci container restart (secrets and AgentCore runtimes do).
- Verified only locally so far: **the `aws` target has never been applied** (no account used); it is validated by
  `terraform validate` only. The first real deployment should start with plan-only.
- Provider quirk found while rehearsing: Floci returns no environment variables, so the module passes `null`
  (not an empty map) when none are set, otherwise the provider reports "inconsistent result after apply".
- The Terraform state is a local file under `deploy/.state/` (git-ignored). Real deployments need a shared remote
  backend (for example S3 with locking); that is not set up here.
- The lock file `deploy/terraform/.terraform.lock.hcl` has hashes for Windows only; add Linux hashes before
  running this in CI (command in `deploy/README.md`).

See also [deploy/README.md](../deploy/README.md) for switches and file layout.
