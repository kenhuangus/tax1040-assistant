"""
P4 — Observation. Per-turn structured events + the two display/audit surfaces.

This is pillar 4. Every turn produces one ``Event`` (``app.models.Event``)
capturing what the harness saw, decided, and did:
    {turn, user_msg, tool_calls[], slot_changes, guardrail_hits, decision, latency_ms}

Two surfaces (prd §4 reuse caveat — do NOT hash the thing you display):
  * DISPLAY surface (``display_events``) — PII-redacted plaintext, served from
    ``GET /trace/{sid}`` and rendered in the UI trace panel. SSNs are masked to
    ``XXX-XX-NNNN`` here; full SSN never appears.
  * AUDIT surface (``audit_record``) — a tamper-evident SHA-256 hash of the
    normalized turn payload with PII redacted *before* hashing (mirrors the
    NovaTax ``auditTrailService`` pattern). The hash is replay/tamper-evidence
    material only; raw PII is never persisted in it.

``build_event(...)`` assembles the typed Event; ``record_event(session, event)``
appends it to the session log (the canonical state mutation for the trace).
``display_events(session)`` returns the redacted view for the API/UI.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from app.guardrails import redact_ssn
from app.models import Decision, Event, GuardrailHit, ToolCall

if False:  # typing only; avoid runtime import cycle
    from app.session import Session


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------
def build_event(
    *,
    turn: int,
    user_msg: str,
    tool_calls: Optional[list[ToolCall]] = None,
    slot_changes: Optional[dict] = None,
    guardrail_hits: Optional[list[GuardrailHit]] = None,
    decision: Optional[Decision] = None,
    latency_ms: int = 0,
) -> Event:
    """Build a typed per-turn ``Event`` (R4.1). All fields default to empty."""
    return Event(
        turn=turn,
        user_msg=user_msg or "",
        tool_calls=list(tool_calls or []),
        slot_changes=dict(slot_changes or {}),
        guardrail_hits=list(guardrail_hits or []),
        decision=decision,
        latency_ms=int(latency_ms),
    )


def record_event(session: "Session", event: Event) -> Event:
    """Append ``event`` to the session's event log and return it."""
    session.events.append(event)
    return event


# ---------------------------------------------------------------------------
# PII redaction for the DISPLAY surface
# ---------------------------------------------------------------------------
# Keys whose VALUES are SSNs / tax identifiers — fully masked on display.
_PII_KEYS = {"ssn", "spouse_ssn", "spousessn", "employee_ssn", "ein", "tin"}


def _redact_value(value: Any) -> Any:
    """Recursively redact SSN-shaped strings and PII-keyed values for display."""
    if value is None:
        return None
    if isinstance(value, str):
        return redact_ssn(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _PII_KEYS and isinstance(v, str):
                out[k] = redact_ssn(v) if v else v
            else:
                out[k] = _redact_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _redact_tool_call(tc: ToolCall) -> dict:
    """Render a ToolCall for display with PII masked (and internal keys dropped)."""
    args = dict(tc.args or {})
    args.pop("_tool_use_id", None)  # internal plumbing, not for display
    return {
        "name": tc.name,
        "args": _redact_value(args),
        "result": _redact_value(tc.result),
        "ok": tc.ok,
    }


# Actions that represent a hard "no" from the harness (guardrail-driven). The UI
# flags these distinctly (warning color) so a judge can see the refusal surface.
_REFUSAL_ACTIONS = {"REFUSE"}


def event_to_display(
    event: Event,
    *,
    session_id: str = "",
    question_budget: Optional[str] = None,
) -> dict:
    """One Event -> a PII-redacted plaintext dict for /trace and the UI panel.

    The projection is shaped so the four harness pillars are each *directly*
    legible to a judge without cross-referencing other fields:

      * Chat loop (P1): ``turn``, ``decision`` (state_before -> state_after,
        next_action + reason) and ``question_budget`` (e.g. ``"2/5"``).
      * Tools (P2): ``tool_calls`` — each ``{name, ok, ...}`` (ok/fail per call),
        plus the ``tools_summary`` rollup (names + an overall ok flag).
      * Guardrails (P3): ``guardrail_hits`` (rule + redacted detail) and the
        ``is_refusal`` flag (a code-level "no" that did not consume a question).
      * Observation (P4): this whole record, PII-redacted, plus ``audit_hash`` —
        the SHA-256 tamper-evidence digest of the turn (raw SSN never hashed).

    ``question_budget`` is supplied by ``display_events`` (it owns the session
    counter); ``session_id`` lets us attach the per-turn audit hash.
    """
    decision = event.decision
    next_action = decision.next_action if decision else None
    hits = [
        {"rule": g.rule, "detail": redact_ssn(g.detail), "slot": g.slot}
        for g in event.guardrail_hits
    ]
    tools = [_redact_tool_call(tc) for tc in event.tool_calls]
    # A refusal is either an explicit REFUSE decision or a "scope" guardrail hit
    # (the deterministic, pre-LLM gate). Either way the UI flags it as a refusal.
    is_refusal = (next_action in _REFUSAL_ACTIONS) or any(
        h["rule"] == "scope" for h in hits
    )
    return {
        "turn": event.turn,
        "user_msg": redact_ssn(event.user_msg),
        "tool_calls": tools,
        # Pillar-2 rollup so the UI needn't recompute it: ordered tool names and
        # whether every call this turn succeeded.
        "tools_summary": {
            "names": [t["name"] for t in tools],
            "ok": all(t["ok"] for t in tools) if tools else True,
            "count": len(tools),
        },
        "slot_changes": _redact_value(event.slot_changes),
        "guardrail_hits": hits,
        "is_refusal": is_refusal,
        "decision": (
            {
                "state_before": decision.state_before,
                "state_after": decision.state_after,
                "next_action": decision.next_action,
                "reason": redact_ssn(decision.reason),
            }
            if decision
            else None
        ),
        # Pillar-1 surface: the live question budget AT this turn (e.g. "2/5").
        "question_budget": question_budget,
        "latency_ms": event.latency_ms,
        # Pillar-4 surface: tamper-evident SHA-256 of the redacted turn payload.
        "audit_hash": audit_record(session_id, event)["payload_hash"],
    }


def display_events(session: "Session") -> list[dict]:
    """The PII-redacted display view of the whole event log (for GET /trace).

    Each turn carries everything the UI needs to render the four pillars (see
    ``event_to_display``), including the running question budget reconstructed
    so the judge sees how many of the ``MAX_QUESTIONS`` were spent by that turn.
    """
    # Import here (not at module top) to avoid any import-order coupling with the
    # state machine while sibling backend files are still being written.
    try:
        from app.statemachine import MAX_QUESTIONS
    except Exception:  # pragma: no cover - statemachine always present in prod
        MAX_QUESTIONS = 5

    sid = getattr(session, "id", "")
    # The session holds only the FINAL question count, so reconstruct the running
    # budget per turn: a turn "spends" a question when it ASKs for a brand-new
    # required slot (refusals/repairs/confirmations/re-asks never count — mirrors
    # the orchestrator's counting rule). We approximate the running count by the
    # number of distinct ASK-for-new-slot turns seen so far, capped at the final.
    final_asked = getattr(session, "questions_asked", 0)
    running = 0
    out: list[dict] = []
    for e in session.events:
        action = e.decision.next_action if e.decision else None
        if action == "ASK" and running < final_asked:
            running += 1
        budget = f"{min(running, final_asked)}/{MAX_QUESTIONS}"
        out.append(event_to_display(e, session_id=sid, question_budget=budget))
    return out


# ---------------------------------------------------------------------------
# AUDIT surface — tamper-evident hash (NovaTax auditTrailService pattern)
# ---------------------------------------------------------------------------
def _redact_for_hash(value: Any) -> Any:
    """Redact PII *before* hashing so the durable digest is not brute-forceable."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _PII_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact_for_hash(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_for_hash(v) for v in value]
    if isinstance(value, str):
        return redact_ssn(value)
    return value


def _stable_json(value: Any) -> str:
    """Deterministic JSON (sorted keys) so identical turns hash identically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def audit_record(session_id: str, event: Event) -> dict:
    """A tamper-evident audit record for a turn.

    The payload is PII-redacted then SHA-256 hashed (replay/tamper evidence
    only — raw SSN is never stored). Mirrors NovaTax ``hashPayload``.
    """
    payload = {
        "session_id": session_id,
        "turn": event.turn,
        "tool_calls": [
            {"name": tc.name, "args": {k: v for k, v in (tc.args or {}).items() if k != "_tool_use_id"}, "ok": tc.ok}
            for tc in event.tool_calls
        ],
        "slot_changes": event.slot_changes,
        "guardrail_hits": [g.rule for g in event.guardrail_hits],
        "next_action": event.decision.next_action if event.decision else None,
    }
    redacted = _redact_for_hash(payload)
    digest = hashlib.sha256(_stable_json(redacted).encode("utf-8")).hexdigest()
    return {
        "session_id": session_id,
        "turn": event.turn,
        "timestamp_ms": int(time.time() * 1000),
        "event_type": event.decision.next_action if event.decision else "turn",
        "payload_hash": digest,
    }


__all__ = [
    "build_event",
    "record_event",
    "event_to_display",
    "display_events",
    "audit_record",
]
