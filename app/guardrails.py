"""
P3 — Guardrails, ENFORCED IN CODE (not the prompt).

This module is pillar 3. Everything here is a deterministic, code-level gate
that runs independently of the LLM:

  * ``scope_check(user_msg, state)`` — runs BEFORE the LLM content call and
    returns a fixed refusal for off-topic / advice-seeking input. It does NOT
    consume a question (the orchestrator never increments the counter when a
    scope hit fires).
  * ``redact_ssn(s)`` — turns any SSN-shaped substring into ``XXX-XX-NNNN``
    (last 4 kept). Used on every user-facing / logged surface.
  * Schema rejection happens in the Pydantic models (``app.models``) and in
    ``w2.parse_w2`` / ``tools.dispatch`` (set_slot validation). A rejected
    parse becomes a ``GuardrailHit(rule="schema")`` in the trace.

The refusal text is fixed and contains the required disclaimer string so the
demo + acceptance test can assert on it verbatim.
"""
from __future__ import annotations

import re

from app.models import ScopeResult

# The single canonical refusal. The acceptance test (prd §5.3) asserts that an
# off-topic / advice request returns this exact disclaimer, and that it does
# NOT consume a question.
REFUSAL_TEXT = (
    "I can only help prepare your 2025 Form 1040 — this is an educational "
    "tool, not tax advice."
)

# ---------------------------------------------------------------------------
# SSN redaction (R3.4). Keep the last 4 digits; mask the first 5.
# ---------------------------------------------------------------------------
# Matches 9 digits with optional separators: 123-45-6789, 123456789, 123 45 6789.
_SSN_RE = re.compile(r"\b(\d{3})[-\s]?(\d{2})[-\s]?(\d{4})\b")


def redact_ssn(s: str) -> str:
    """Redact every SSN-shaped run in ``s`` to ``XXX-XX-NNNN`` (last 4 kept).

    Safe on ``None``/non-str (returns it stringified or empty) and idempotent —
    a value already redacted has no 9-digit run left to match.
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return _SSN_RE.sub(lambda m: f"XXX-XX-{m.group(3)}", s)


# ---------------------------------------------------------------------------
# Scope gate (R3.2) — deterministic, pre-LLM.
# ---------------------------------------------------------------------------
# Advice-seeking intent: the user wants tax *advice* / optimization / a legal or
# financial opinion. We are an educational filing tool, so we refuse and do NOT
# burn a question. Phrased as word-boundary patterns to avoid over-matching
# (e.g. "should" alone is fine; "should I" asking for a recommendation is not).
_ADVICE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bgive me .*advice\b",
        r"\btax advice\b",
        r"\blegal advice\b",
        r"\bfinancial advice\b",
        r"\bshould i (?:invest|buy|sell|claim|deduct|itemize|incorporate|donate|contribute|file as|elect|convert)\b",
        r"\bhow (?:can|do) i (?:avoid|reduce|lower|minimize|evade|dodge|cut) .*\b(?:tax|taxes)\b",
        r"\b(?:avoid|evade|dodge|cheat on|get out of) .*\b(?:tax|taxes|irs)\b",
        r"\bbest way to .*\b(?:save|reduce|lower|minimize) .*\b(?:tax|taxes)\b",
        r"\bwhat (?:should|would) you (?:recommend|advise|suggest)\b",
        r"\bis it (?:better|worth it|smart|a good idea) to\b",
        r"\bwrite[- ]?off\b",
        r"\bloophole\b",
        r"\btax shelter\b",
    )
)

# Clearly off-topic intent: not about preparing this 1040 at all. Kept narrow
# so normal tax-prep chatter is never refused (the LLM + state machine handle
# legitimate on-topic turns).
_OFFTOPIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bweather\b",
        r"\bwrite (?:me )?(?:a |an )?(?:poem|song|story|essay|joke|haiku|code|script|program)\b",
        r"\b(?:who|what|when|where) (?:is|was|are|were) (?:the )?(?:president|capital|weather|stock price|score)\b",
        r"\bstock (?:price|market|tip|pick)\b",
        r"\bcrypto(?:currency)? (?:price|advice|investment)\b",
        r"\brecipe\b",
        r"\bbitcoin\b",
        r"\bwho (?:won|will win)\b",
        r"\btell me a joke\b",
        r"\bwhat (?:is|are) you\b",
        r"\bignore (?:your |the |all )?(?:previous |prior )?instructions\b",  # prompt-injection
        r"\bsystem prompt\b",
    )
)


def scope_check(user_msg: str, state: str) -> ScopeResult:
    """Deterministic, pre-LLM scope gate (R3.2).

    Returns ``ScopeResult(ok=False, refusal=REFUSAL_TEXT, rule="scope")`` when
    the message is advice-seeking or clearly off-topic; otherwise
    ``ScopeResult(ok=True)``.

    The orchestrator calls this BEFORE any LLM content call. A False result is
    emitted as a ``GuardrailHit`` and short-circuits the turn — crucially WITHOUT
    incrementing ``questions_asked`` (the refusal is not a question).

    ``state`` is accepted for forward-compatibility (per-state scoping) but the
    core rules are state-independent: advice and off-topic are always refused.
    """
    msg = (user_msg or "").strip()
    if not msg:
        # Empty input isn't off-topic; let the state machine re-prompt.
        return ScopeResult(ok=True)

    for pat in _ADVICE_PATTERNS:
        if pat.search(msg):
            return ScopeResult(ok=False, refusal=REFUSAL_TEXT, rule="scope")
    for pat in _OFFTOPIC_PATTERNS:
        if pat.search(msg):
            return ScopeResult(ok=False, refusal=REFUSAL_TEXT, rule="scope")

    return ScopeResult(ok=True)


__all__ = ["scope_check", "redact_ssn", "REFUSAL_TEXT"]
