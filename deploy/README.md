# deploy/ — deployment rehearsal (RUN-05)

The **same Terraform** deploys an agent to real AWS or to the local Floci emulator. A variable `target`
(`local` | `aws`) switches only the provider settings; the resources are identical. Plain-language guide:
[docs/deployment-rehearsal.md](../docs/deployment-rehearsal.md).

## What it creates (from `agent.yaml`)
| Resource | Notes |
|----------|-------|
| `aws_secretsmanager_secret` per manifest `secrets` entry | Placeholder value, `ignore_changes = [secret_string]`: set the real value out of band |
| `aws_s3_bucket`, `aws_dynamodb_table` | Optional (`s3_bucket_name`, `dynamodb_table_name`) |
| `aws_iam_role` + inline policy | Least privilege: only this agent's secrets/bucket/table + logs |
| `aws_bedrockagentcore_agent_runtime` | Container artifact, `PUBLIC` network, `HTTP` protocol. Name = manifest name with `-` -> `_` |

Tags on everything: `team`, `data-classification`, `environment`, `agent`, `managed-by`.

## Run it (repo root, PowerShell)
```powershell
. .\harness\env.ps1                                   # puts terraform on PATH (once per session)
# Plan only (changes nothing):
.\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent
# Create + smoke checks:
.\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent -Apply
# Remove it again:
.\deploy\deploy.ps1 -Target local -Manifest ..\my-agent\agent.yaml -NameOverride rehearsal-agent -Destroy
```
Real AWS (needs your normal AWS credentials, an ECR image, and the explicit confirmation switch):
```powershell
.\deploy\deploy.ps1 -Target aws -Manifest agent.yaml -Image <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag> -Apply -ConfirmRealAws
```

| Switch | Meaning |
|--------|---------|
| `-Target local\|aws` | Where to deploy (required) |
| `-Manifest <path>` | The agent's `agent.yaml` (validated with the SDK's own parser) |
| `-Image <uri>` | Container image. Local default: `local/<name>:dev` (Floci never pulls it). Required for `aws` |
| `-NameOverride <name>` | Deploy under another name (so a rehearsal can't clash with a real agent) |
| `-ExtraSecrets`, `-Bucket`, `-Table` | Rehearse more resources than the manifest lists |
| `-Apply` / `-Destroy` | Without either: plan only. `aws` + either needs `-ConfirmRealAws` |

Exit code is non-zero if any step or smoke check fails. Smoke checks: secrets exist, bucket/table exist (if
requested), runtime status is `READY` and network mode `PUBLIC`, and (local) the invoke router on `:4567` lists the
runtime. After `-Destroy` the checks confirm the resources are gone.

## Files
- `terraform/` — root module (`versions.tf` pins `hashicorp/aws ~> 6.0`; `.terraform.lock.hcl` is committed).
- `deploy.ps1` — wrapper. Generates `.state/<target>-<name>/agent.auto.tfvars.json` and keeps one state file per
  target + agent in `.state/` (git-ignored).

## Notes
- The provider is large (~800 MB unpacked per platform). `deploy.ps1` uses a shared plugin cache
  (`%LOCALAPPDATA%\terraform-plugin-cache`) so it is downloaded once.
- The lock file currently has hashes for `windows_amd64` only. Before CI on Linux run
  `terraform -chdir=deploy/terraform providers lock -platform=windows_amd64 -platform=linux_amd64 -platform=linux_arm64`
  and commit the result.
- Real AWS state should move to a remote backend (S3 + locking). The local file backend is for rehearsal.
