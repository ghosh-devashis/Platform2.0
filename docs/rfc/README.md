# Standards change process (RFC)

How a rule, a default or a platform behaviour changes (GOV-04). The point: nobody is surprised by a new rule, and
security, platform and agent developers all get a say before it starts failing builds.

## When you need an RFC

- Adding, removing or tightening a policy rule (`ENT-0xx`), a guardrail default, or an approved-dependency/model list entry
  that affects many repos.
- Changing the SDK's public API, the agent contract, the manifest format, or the supported-version policy.
- Anything that can turn a passing build into a failing one.

Bug fixes, new documentation and additive features that change no existing behaviour don't need one.

## The process

1. **Draft**: copy [0000-template.md](0000-template.md) to `docs/rfc/NNNN-short-title.md` (next free number), fill it in, open a pull request. Status `Draft`.
2. **Review (at least 5 working days)**: it needs a written response from each of the three groups: **platform** (owns the SDK and pipeline),
   **security** (owns rules and waivers) and **agent developers** (at least two teams that will live with it). Discussion happens on the PR.
3. **Decide**: the platform lead merges when all three groups have approved or their objections are recorded and answered. Status becomes `Accepted` or `Rejected`, with the reason.
4. **Announce**: a post in the platform channel and a [CHANGELOG](../../CHANGELOG.md) entry. For anything that can fail builds, the rule ships **first as a warning** (ENT-0xx severity `warning`) for one release and becomes an error in the next, so teams have time to fix.
5. **Ship and measure**: after a release, check how many repos the change affects (the [compliance dashboard](../../policy/tools/build_register.py)) and record the outcome in the RFC.

## Urgent security fixes

A rule needed now to stop an active risk can be merged first and reviewed after: the RFC is opened the same day, status `Accepted (expedited)`, and reviewed within five working days. Waivers (`waivers.yaml`) cover repos that cannot comply immediately.

## Records

- `Draft` → `Accepted` / `Rejected` / `Superseded by NNNN`. Accepted RFCs are never edited except to mark them superseded.
- Every rule in [rules.md](../../policy/docs/rules.md) should be traceable to an RFC (or to the original requirements spec) in its "Why".
