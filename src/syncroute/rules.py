"""High-precision deterministic rules.

Every rule here fires on *explicit structured evidence* or on a narrowly
scoped, documented signature. Each carries an assumption string that says what
it is taking for granted.

Deliberate non-rules, because provider behaviour makes them ambiguous:

* HTTP 401 does not imply REAUTHENTICATE. Several providers return 401 for a
  missing scope, and some return it for a revoked grant. Judgement required.
* HTTP 403 does not imply FIX_PERMISSIONS. It is used for revoked tokens,
  IP allow-lists and disabled accounts as well as missing grants.
* HTTP 404 does not imply REVIEW_CONFIGURATION. Some APIs return 404 for
  objects the caller merely cannot see.
* A timeout does not imply CHECK_CONNECTIVITY. Slow queries time out too.
* FIX_PERMISSIONS intentionally has no deterministic rule. Distinguishing "the
  identity lacks a grant" from "the credential is bad" needs the error language,
  which is what Jev is for.

This set is frozen before held-out evaluation. Do not extend it to match
held-out cases; that would make the baseline dishonest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .models import (
    AuthState,
    NetworkProbeState,
    ReplicationPositionState,
    ResourceValidationState,
    Route,
    SyncFailureEvent,
)
from .policy import PolicyFacts

RULES_VERSION = "rules-2026.09.1"

# Narrow: requires the word "revoked" next to a credential noun. Wording such as
# "consent was withdrawn" is deliberately NOT matched; that is model territory.
_REVOKED_CREDENTIAL = re.compile(
    r"(?i)\b(refresh (?:credential|token)|credential|access token|api key|oauth token)\b"
    r"[^.!?]{0,60}?\brevoked\b"
)
_REVOKED_CREDENTIAL_REVERSED = re.compile(
    r"(?i)\brevoked\b[^.!?]{0,40}?\b(refresh (?:credential|token)|credential|access token|api key)\b"
)

_PROVIDER_REAUTH_CODES = frozenset({"INVALID_GRANT", "TOKEN_REVOKED", "CREDENTIAL_REVOKED"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    route: Route
    description: str
    assumption: str
    predicate: Callable[[SyncFailureEvent, PolicyFacts], bool]


@dataclass(frozen=True)
class RuleResult:
    """Outcome of the deterministic stage."""

    route: Optional[Route]
    rule_id: Optional[str]
    matched: tuple[str, ...] = ()
    conflict: bool = False
    reason: str = ""


def _http_status_in(*codes: int) -> Callable[[SyncFailureEvent, PolicyFacts], bool]:
    allowed = frozenset(codes)
    return lambda event, facts: event.http_status in allowed


RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="R001_HTTP_429_RATE_LIMIT",
        route=Route.RETRY_LATER,
        description="HTTP 429 from the provider.",
        assumption="429 is a rate limit across the providers modelled here; the retry budget is checked separately by policy.",
        predicate=_http_status_in(429),
    ),
    Rule(
        rule_id="R002_HTTP_503_UNAVAILABLE",
        route=Route.RETRY_LATER,
        description="HTTP 503 from the provider.",
        assumption="503 indicates a service-side condition expected to clear; it is not used here to signal a configuration problem.",
        predicate=_http_status_in(503),
    ),
    Rule(
        rule_id="R003_HTTP_GATEWAY_TRANSIENT",
        route=Route.RETRY_LATER,
        description="HTTP 502 or 504 from the provider or its gateway.",
        assumption="Gateway-level 502/504 responses reflect a transient upstream condition rather than a client error.",
        predicate=_http_status_in(502, 504),
    ),
    Rule(
        rule_id="R010_AUTH_STATE_REVOKED",
        route=Route.REAUTHENTICATE,
        description="Structured auth_state is 'revoked'.",
        assumption="auth_state is set by a credential check, not inferred from the error text.",
        predicate=lambda event, facts: event.auth_state is AuthState.REVOKED,
    ),
    Rule(
        rule_id="R011_AUTH_STATE_EXPIRED",
        route=Route.REAUTHENTICATE,
        description="Structured auth_state is 'expired'.",
        assumption="An expired credential needs refreshing or reconnecting regardless of the failing operation.",
        predicate=lambda event, facts: event.auth_state is AuthState.EXPIRED,
    ),
    Rule(
        rule_id="R012_EXPLICIT_REVOKED_CREDENTIAL_TEXT",
        route=Route.REAUTHENTICATE,
        description="Error text explicitly states a credential was revoked.",
        assumption="The word 'revoked' adjacent to a credential noun is an explicit provider statement, not a paraphrase.",
        predicate=lambda event, facts: bool(
            _REVOKED_CREDENTIAL.search(event.error_message)
            or _REVOKED_CREDENTIAL_REVERSED.search(event.error_message)
        ),
    ),
    Rule(
        rule_id="R013_PROVIDER_REAUTH_CODE",
        route=Route.REAUTHENTICATE,
        description="Provider error code is a standard grant-invalidation code.",
        assumption="These OAuth-style codes are returned when the stored grant can no longer be exchanged, so reconnecting is the next action.",
        predicate=lambda event, facts: bool(
            event.provider_error_code
            and event.provider_error_code.strip().upper() in _PROVIDER_REAUTH_CODES
        ),
    ),
    Rule(
        rule_id="R020_REPLICATION_POSITION_EXPIRED",
        route=Route.REVIEW_RESYNC,
        description="Structured replication_position_state is 'expired'.",
        assumption="The stored cursor was checked against retained logs; continuation is not possible from it.",
        predicate=lambda event, facts: (
            event.replication_position_state is ReplicationPositionState.EXPIRED
        ),
    ),
    Rule(
        rule_id="R030_NETWORK_PROBE_UNREACHABLE",
        route=Route.CHECK_CONNECTIVITY,
        description="Structured network_probe_state is 'unreachable'.",
        assumption="An independent probe, not the error text, established unreachability.",
        predicate=lambda event, facts: event.network_probe_state is NetworkProbeState.UNREACHABLE,
    ),
    Rule(
        rule_id="R040_RESOURCE_VALIDATED_MISSING",
        route=Route.REVIEW_CONFIGURATION,
        description="Structured resource_validation_state is 'missing'.",
        assumption="Resource listing succeeded and the configured object was absent, so the identity could see the namespace.",
        predicate=lambda event, facts: (
            event.resource_validation_state is ResourceValidationState.MISSING
        ),
    ),
)

RULES_BY_ID = {rule.rule_id: rule for rule in RULES}


def evaluate_rules(event: SyncFailureEvent, facts: PolicyFacts) -> RuleResult:
    """Apply every rule. Conflicting outcomes escalate rather than guess."""
    matches = [rule for rule in RULES if rule.predicate(event, facts)]
    if not matches:
        return RuleResult(route=None, rule_id=None, reason="No deterministic rule matched.")

    routes = {rule.route for rule in matches}
    matched_ids = tuple(rule.rule_id for rule in matches)
    if len(routes) > 1:
        conflicting = ", ".join(f"{r.rule_id}->{r.route.value}" for r in matches)
        return RuleResult(
            route=Route.ENGINEER_REVIEW,
            rule_id="CONFLICT",
            matched=matched_ids,
            conflict=True,
            reason=(
                "Deterministic rules disagreed about the next action "
                f"({conflicting}), so the evidence does not resolve to one workflow."
            ),
        )

    winner = matches[0]
    return RuleResult(
        route=winner.route,
        rule_id=winner.rule_id,
        matched=matched_ids,
        reason=f"{winner.description} {winner.assumption}",
    )
