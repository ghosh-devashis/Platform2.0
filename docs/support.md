# Platform support (NFR-06)

How agent teams get help, and what the platform team commits to. **Fill in the bracketed items for your organisation**:
the structure and targets below follow the requirements spec, the names and channels are yours to set.

## Where to ask

| You need | Go to |
|----------|-------|
| A question, "how do I...?" | Platform channel `[#platform-agents]` |
| A bug, or a rule that blocks a build wrongly | A ticket in `[the platform issue tracker]`, label `blocking-rule` if CI is blocked |
| A waiver or an exception | Open a PR adding it to `waivers.yaml`; approver is `[security approver group]` |
| Approval of a new dependency or model | A PR to `policy/data/approved-dependencies.json` / `approved-models.json` (see [the RFC process](rfc/README.md) if it affects many repos) |
| A security incident involving an agent | `[security on-call]`, not the normal channel |

## What to include in a report

Faster answers come with: the **trace ID** (`x-ent-trace-id` response header, or from Jaeger), the output of
`uv run ent-agent metadata` (SDK and policy versions), the rule ID that failed (`ENT-0xx`), and the smallest repro.
Never paste secrets, prompts or personal data: the SDK's logs and traces are designed not to contain them, so a trace ID is enough.

## Targets

| Severity | Example | First response | Target resolution or workaround |
|----------|---------|----------------|---------------------------------|
| **S1** Production agents down or unsafe | Gateway outage, guardrail bypass | 30 minutes, 24/7 `[on-call]` | 4 hours |
| **S2** Blocking rule or pipeline fault | A rule fails wrongly and blocks many repos | **1 business day** (spec NFR-06) | 3 business days, or a waiver/hotfix the same day |
| **S3** Bug with a workaround | Wrong error message, flaky tool | 2 business days | Next release |
| **S4** Question or request | Template improvement | 3 business days | Planned via the backlog or an RFC |

## What the platform team owns, and what it doesn't

- **Owns**: the SDK, the policy bundle and pipeline, the template, the VS Code extension and MCP server, the local runtime and the gateway deployment.
- **Doesn't own**: your agent's logic, your tools' backends, or your prompts. We help you diagnose using traces and audit events.

## Staying supported

Keep to the supported release window: the current and previous minor series of the SDK and policy bundle (see
[the compatibility matrix](compatibility-matrix.md)). Repos past a sunset date fail CI (ENT-060) and can't be helped beyond upgrading.

## Being on the platform team's radar

- Release notes: [CHANGELOG](../CHANGELOG.md). Upcoming rule changes appear as warnings one release before they become errors.
- Health dashboards: the compliance dashboard (`policy/tools/build_register.py`) and `[the observability dashboards]`.
