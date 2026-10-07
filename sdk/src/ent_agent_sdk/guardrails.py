"""Guardrails (SDK-08, layer 2): checks the gateway can't see.

Portkey's gateway guardrails (layer 1) check every LLM request and response. This layer covers the rest:

- `input`       the user's request
- `tool_input`  arguments the model chose for a tool
- `tool_output` what a tool or retrieval returned (the main route for prompt injection)
- `output`      the final answer returned to the caller

Built-in detectors find personal data (`pii`), credentials (`secret`) and injection attempts (`injection`).
A profile decides, per category and stage, whether to allow, redact or block. `standard` suits most agents;
`strict` is required for `restricted` data (see manifest.py). `BedrockGuardrail` adds Amazon Bedrock Guardrails as
an extra check where the enterprise mandates it.

Fail closed: a guardrail that can't run blocks the request. Disabling guardrails needs an approved waiver ID
(`Guardrails.off(waiver=...)`); the policy bundle's waiver check verifies it, and an audit event is emitted.
Detection here is pattern-based: it is a safety net, not a replacement for the gateway or Bedrock layers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from ent_agent_sdk import audit

logger = logging.getLogger("ent_agent_sdk.guardrails")

STAGES = ("input", "tool_input", "tool_output", "output")


class Action(str, Enum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


class GuardrailViolation(Exception):
    """A guardrail blocked the content. The message names the stage and categories, never the content."""

    def __init__(self, stage: str, categories: Sequence[str]) -> None:
        self.stage = stage
        self.categories = sorted(set(categories))
        super().__init__(f"Blocked by guardrail policy at stage '{stage}' ({', '.join(self.categories)}).")


class GuardrailUnavailable(GuardrailViolation):
    """An external guardrail could not be reached; the request is blocked (fail closed)."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage, ["unavailable"])


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    category: str
    rule: str


# --- Detectors -------------------------------------------------------------------------------------------------

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")

_SECRETS = {
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
}

_INJECTION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts|messages|rules)",
        r"disregard (?:all |any |the )?(?:previous |prior |above |earlier )?(?:instructions|rules|guidelines)",
        r"(?:forget|override) (?:all |your )?(?:previous |prior )?(?:instructions|rules)",
        r"(?:reveal|show|print|repeat) (?:me )?(?:your|the) (?:system|hidden|initial) (?:prompt|instructions)",
        r"you are now (?:in )?(?:developer mode|dan\b|jailbroken)",
        r"\bnew instructions\s*:",
    )
]


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def detect(text: str) -> list[Match]:
    """All matches of the built-in detectors in `text`."""
    matches: list[Match] = []
    for m in _EMAIL.finditer(text):
        matches.append(Match(m.start(), m.end(), "pii", "email"))
    for m in _SSN.finditer(text):
        matches.append(Match(m.start(), m.end(), "pii", "us-ssn"))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            matches.append(Match(m.start(), m.end(), "pii", "payment-card"))
    for m in _PHONE.finditer(text):
        matches.append(Match(m.start(), m.end(), "pii", "phone"))
    for rule, pattern in _SECRETS.items():
        for m in pattern.finditer(text):
            matches.append(Match(m.start(), m.end(), "secret", rule))
    for pattern in _INJECTION:
        for m in pattern.finditer(text):
            matches.append(Match(m.start(), m.end(), "injection", "prompt-injection"))
    return matches


# --- Profiles --------------------------------------------------------------------------------------------------

def _row(input_: Action, tool_input: Action, tool_output: Action, output: Action) -> dict[str, Action]:
    return {"input": input_, "tool_input": tool_input, "tool_output": tool_output, "output": output}


A = Action
PROFILES: dict[str, dict[str, dict[str, Action]]] = {
    "standard": {
        "pii": _row(A.ALLOW, A.ALLOW, A.REDACT, A.REDACT),
        "secret": _row(A.REDACT, A.BLOCK, A.REDACT, A.REDACT),
        "injection": _row(A.ALLOW, A.ALLOW, A.BLOCK, A.ALLOW),
    },
    "strict": {
        "pii": _row(A.BLOCK, A.BLOCK, A.REDACT, A.BLOCK),
        "secret": _row(A.BLOCK, A.BLOCK, A.BLOCK, A.BLOCK),
        "injection": _row(A.BLOCK, A.BLOCK, A.BLOCK, A.ALLOW),
    },
}

ExtraCheck = Callable[[str, str], str]  # (text, stage) -> text; raise GuardrailViolation to block


class Guardrails:
    """Applies a profile's rules to text at each stage of an invocation."""

    def __init__(
        self,
        profile: str = "standard",
        *,
        extra_checks: Sequence[ExtraCheck] = (),
        enabled: bool = True,
        waiver: str | None = None,
    ) -> None:
        if profile not in PROFILES:
            raise ValueError(f"Unknown guardrail profile '{profile}'. Choose from {sorted(PROFILES)}.")
        self.profile = profile
        self.extra_checks = tuple(extra_checks)
        self.enabled = enabled
        self.waiver = waiver

    @classmethod
    def off(cls, *, waiver: str) -> Guardrails:
        """Disable guardrails. Requires an approved waiver ID from waivers.yaml (checked in CI)."""
        if not waiver or not waiver.strip():
            raise ValueError("Disabling guardrails requires an approved waiver ID (see waivers.yaml).")
        return cls(enabled=False, waiver=waiver.strip())

    @property
    def disabled(self) -> bool:
        return not self.enabled

    def enforce(self, text: str, stage: str) -> str:
        """Return `text` (redacted where the profile says so); raise `GuardrailViolation` if it must be blocked."""
        if not self.enabled:
            return text
        if stage not in STAGES:
            raise ValueError(f"Unknown guardrail stage '{stage}'.")

        rules = PROFILES[self.profile]
        matches = detect(text)
        blocked = sorted({m.category for m in matches if rules[m.category][stage] is Action.BLOCK})
        if blocked:
            audit.emit(
                audit.GUARDRAIL_BLOCK, outcome="blocked", stage=stage, profile=self.profile,
                categories=",".join(blocked), rules=",".join(sorted({m.rule for m in matches if m.category in blocked})),
            )
            raise GuardrailViolation(stage, blocked)

        redactions = [m for m in matches if rules[m.category][stage] is Action.REDACT]
        if redactions:
            text = _redact(text, redactions)
            audit.emit(
                audit.GUARDRAIL_REDACT, outcome="redacted", stage=stage, profile=self.profile,
                categories=",".join(sorted({m.category for m in redactions})), count=len(redactions),
            )

        for check in self.extra_checks:
            try:
                text = check(text, stage)
            except GuardrailViolation as exc:
                audit.emit(audit.GUARDRAIL_BLOCK, outcome="blocked", stage=stage, profile=self.profile,
                           categories=",".join(exc.categories), rules="external")
                raise
        return text

    def enforce_payload(self, value: Any, stage: str) -> Any:
        """Apply `enforce` to every string inside dicts, lists and tuples; other values pass through."""
        if not self.enabled:
            return value
        if isinstance(value, str):
            return self.enforce(value, stage)
        if isinstance(value, Mapping):
            return {k: self.enforce_payload(v, stage) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.enforce_payload(v, stage) for v in value)
        return value


def _redact(text: str, matches: Sequence[Match]) -> str:
    """Replace matched spans with [REDACTED:category], merging overlaps."""
    spans: list[list[Any]] = []
    for m in sorted(matches, key=lambda x: (x.start, -x.end)):
        if spans and m.start < spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], m.end)
        else:
            spans.append([m.start, m.end, m.category])
    for start, end, category in reversed(spans):
        text = f"{text[:start]}[REDACTED:{category}]{text[end:]}"
    return text


class BedrockGuardrail:
    """Amazon Bedrock Guardrails (ApplyGuardrail) as an extra check. Any intervention blocks; failure blocks too."""

    def __init__(self, guardrail_id: str, version: str = "DRAFT", *, session: Any = None) -> None:
        import boto3

        from ent_agent_sdk.secrets import BOTO_CONFIG  # same short timeouts and bounded retries (SDK-16)

        self.guardrail_id = guardrail_id
        self.version = version
        self._client = (session or boto3.session.Session()).client("bedrock-runtime", config=BOTO_CONFIG)

    def __call__(self, text: str, stage: str) -> str:
        source = "INPUT" if stage in ("input", "tool_input") else "OUTPUT"
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self.guardrail_id, guardrailVersion=self.version, source=source,
                content=[{"text": {"text": text}}],
            )
        except (BotoCoreError, ClientError):
            logger.exception("Bedrock guardrail call failed; blocking (fail closed)")
            raise GuardrailUnavailable(stage) from None
        if response.get("action") == "GUARDRAIL_INTERVENED":
            raise GuardrailViolation(stage, ["bedrock"])
        return text
