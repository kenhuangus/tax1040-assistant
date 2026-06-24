"""
P1 — Server-held, typed per-session state + in-memory store.

This is pillar 1's data layer. The server (never the model, never the client)
owns the conversation state as a typed object. State is carried across turns by
looking the session up in ``STORE`` by ``session_id`` on every ``POST /chat``.

  * ``Slots`` — the validated facts we collect: filing_status, w2, dependents,
    plus a ``confirmed`` flag (set when the user OKs the final confirmation).
  * ``Session`` — id, slots, event log, the question counter, per-slot repair
    counters, the compute ``result``, the filled ``pdf_path``, and the current
    state-machine ``state``.
  * ``STORE`` / ``get_or_create`` — the in-memory session registry. Pinned to a
    single Cloud Run instance in prod so state survives across turns (R1.3).

Money stays whole-dollar int; SSN lives raw only inside ``W2`` (for the final
PDF) and is redacted on every display surface by ``trace.py`` /
``guardrails.redact_ssn``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.models import Dependent, Event, Form1040Result, W2

# The seven canonical states (mirrors statemachine.py). Initial state asks for
# the W-2 first because it supplies wages, withholding AND identity.
INITIAL_STATE = "AWAIT_W2"


@dataclass
class Slots:
    """The validated facts we collect from the conversation.

    A slot is "filled" only when it holds a schema-valid value. ``w2`` carries
    wages + fed_withholding + identity in one shot (so it satisfies several
    required slots at once). ``dependents`` is required-but-defaultable: an
    empty list means "no dependents" once the user has answered.
    """

    filing_status: Optional[str] = None
    w2: Optional[W2] = None
    dependents: list[Dependent] = field(default_factory=list)
    # True once the user has explicitly answered the dependents question, so we
    # can distinguish "not asked yet" (None-ish) from "answered: none".
    dependents_answered: bool = False
    # True once the user confirms the CONFIRM_FACTS summary.
    confirmed: bool = False


@dataclass
class Session:
    """Per-session typed state. Owned by the server; survives across turns."""

    id: str
    slots: Slots = field(default_factory=Slots)
    events: list[Event] = field(default_factory=list)
    questions_asked: int = 0
    # Per-slot repair counter (R3.3 — bounded by max_repairs_per_slot=2,
    # SEPARATE from the question budget so repairs never burn questions).
    repairs: dict[str, int] = field(default_factory=dict)
    # The set of required slots we have ALREADY counted a question for. This
    # makes the question count idempotent per slot: re-prompting the same
    # unfilled slot (a retry) must NOT increment again (prd §4 counting rule).
    asked_slots: set[str] = field(default_factory=set)
    result: Optional[Form1040Result] = None
    pdf_path: Optional[str] = None
    state: str = INITIAL_STATE
    # Running conversation history for the LLM (anthropic message dicts). Server
    # holds this so multi-turn context is carried across POST /chat calls.
    history: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# In-memory store (instance-pinned in prod via --min-instances=1 --max-instances=1).
# ---------------------------------------------------------------------------
STORE: dict[str, Session] = {}


def get_or_create(sid: Optional[str]) -> Session:
    """Return the existing session for ``sid`` or create a fresh one.

    Carrying state across turns (P1) is literally this lookup: the client sends
    back the ``session_id`` it received, and we return the SAME ``Session``
    object — slots, counters, history and all — so the conversation continues
    instead of resetting to question 1.
    """
    if sid and sid in STORE:
        return STORE[sid]
    new_id = sid if sid else uuid.uuid4().hex
    sess = Session(id=new_id)
    STORE[new_id] = sess
    return sess


__all__ = ["Slots", "Session", "STORE", "get_or_create", "INITIAL_STATE"]
