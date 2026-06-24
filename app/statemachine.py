"""
The state machine — pillar 1's flow control + pillar 3's budget gate.

``next_action(session) -> Decision`` is a PURE function: it reads the filled +
valid slots and returns exactly one next action with a human-readable reason.
The orchestrator (not the model) drives the conversation off this decision.

States:
    AWAIT_W2 -> AWAIT_FILING_STATUS -> AWAIT_DEPENDENTS -> CONFIRM_FACTS
             -> COMPUTING -> READY_DOWNLOAD ; plus REPAIR.

Actions returned in ``Decision.next_action``:
    ASK      — solicit a new required slot (the ONLY action that can count a
               question).
    CONFIRM  — all required slots filled; ask the user to confirm before compute.
    COMPUTE  — run compute_1040 (then fill happens on download). Reached when the
               user confirms, OR forced by the budget gate.
    REPAIR   — a slot is present but invalid / the user wants to fix it (bounded
               by max_repairs_per_slot, does NOT count a question).
    REFUSE   — reserved; scope refusals are handled in the orchestrator before
               the LLM call, so this is rarely emitted here.

QUESTION-COUNTING RULE (prd §4, enforced here + in the orchestrator):
  A question is counted ONCE, on entry to an ASK action for an *unfilled required
  slot we have not asked about before*. The state machine exposes which slot the
  ASK targets (``Decision.reason`` names it and the orchestrator reads
  ``slot_for_decision``); the orchestrator increments the counter only if that
  slot is not already in ``session.asked_slots``. Confirmations, repairs,
  refusals and same-slot re-prompts never increment.

BUDGET GATE (R3.3): the hard cap is 5. The orchestrator applies it as a
precondition — ``if next_action == ASK and questions_asked >= 5: force COMPUTE``.
``next_action`` itself also honors the cap so the pure decision is consistent.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.models import Decision

if TYPE_CHECKING:  # avoid a runtime import cycle (session imports nothing here)
    from app.session import Session

# --- constants ----------------------------------------------------------------
MAX_QUESTIONS = 5
MAX_REPAIRS_PER_SLOT = 2

# Required slots, in the order we collect them. Each maps to the state that asks
# for it. ``dependents`` is required-but-defaultable (answering "none" fills it).
STATES = (
    "AWAIT_W2",
    "AWAIT_FILING_STATUS",
    "AWAIT_DEPENDENTS",
    "CONFIRM_FACTS",
    "COMPUTING",
    "READY_DOWNLOAD",
    "REPAIR",
)

VALID_FILING_STATUSES = {"single", "mfj", "mfs", "hoh", "qss"}


def _w2_filled(session: "Session") -> bool:
    """True iff the W-2 slot holds a schema-valid W2 with the required money."""
    w2 = session.slots.w2
    return (
        w2 is not None
        and isinstance(w2.wages, int)
        and w2.wages >= 0
        and isinstance(w2.fed_withholding, int)
        and w2.fed_withholding >= 0
    )


def _filing_status_filled(session: "Session") -> bool:
    return session.slots.filing_status in VALID_FILING_STATUSES


def _dependents_filled(session: "Session") -> bool:
    """Required-but-defaultable: filled once the user has answered (even 'none')."""
    return session.slots.dependents_answered


def first_unfilled_required_slot(session: "Session") -> Optional[str]:
    """Return the name of the first unfilled required slot, or None if all filled.

    Order: w2 -> filing_status -> dependents. This is what an ASK targets and
    what the question counter keys on.
    """
    if not _w2_filled(session):
        return "w2"
    if not _filing_status_filled(session):
        return "filing_status"
    if not _dependents_filled(session):
        return "dependents"
    return None


# Map each required slot to the state that solicits it.
_SLOT_TO_ASK_STATE = {
    "w2": "AWAIT_W2",
    "filing_status": "AWAIT_FILING_STATUS",
    "dependents": "AWAIT_DEPENDENTS",
}


def _domain_repair(session: "Session") -> Optional[str]:
    """Domain guardrail (architecture §3 stretch): HoH / QSS require >=1 dependent.

    If the user has claimed HoH/QSS but recorded zero dependents AFTER answering
    the dependents question, that claim is invalid -> REPAIR the filing_status
    (bounded by the repair budget). Returns a reason string or None.
    """
    fs = session.slots.filing_status
    if (
        fs in ("hoh", "qss")
        and session.slots.dependents_answered
        and len(session.slots.dependents) == 0
    ):
        return (
            f"filing status '{fs}' requires at least one qualifying dependent, "
            f"but none were recorded"
        )
    return None


def next_action(session: "Session") -> Decision:
    """Pure decision: given the current session, what should happen next?

    Returns a ``Decision`` whose ``next_action`` is one of
    ASK/CONFIRM/COMPUTE/REPAIR/REFUSE and whose ``reason`` is human-readable
    (it surfaces in the trace, R4.2). ``state_before`` is the session's current
    state; ``state_after`` is where this decision moves us.

    The orchestrator is responsible for the actual counter increment and for the
    "force COMPUTE at the cap" side effect; this function reflects the same cap
    so the returned decision is never inconsistent with the gate.
    """
    state_before = session.state
    budget = f"{session.questions_asked}/{MAX_QUESTIONS}"

    # 0) Already computed -> we're done; sit in READY_DOWNLOAD.
    if session.result is not None:
        return Decision(
            state_before=state_before,
            state_after="READY_DOWNLOAD",
            next_action="COMPUTE",
            reason=f"result already computed; download unlocked (budget {budget})",
        )

    # 1) Domain repair takes priority (invalid HoH/QSS claim).
    dom = _domain_repair(session)
    if dom is not None:
        slot = "filing_status"
        used = session.repairs.get(slot, 0)
        if used < MAX_REPAIRS_PER_SLOT:
            return Decision(
                state_before=state_before,
                state_after="REPAIR",
                next_action="REPAIR",
                reason=f"{dom}; repair {used + 1}/{MAX_REPAIRS_PER_SLOT} (no question consumed)",
            )
        # Repair budget exhausted -> treat as unfilled and re-ask filing status.
        # (Falls through to the ASK path below.)

    # 2) Collect required slots in order.
    slot = first_unfilled_required_slot(session)
    if slot is not None:
        ask_state = _SLOT_TO_ASK_STATE[slot]
        # Budget gate (R3.3): if we'd ask a NEW question but the cap is hit,
        # force COMPUTE with whatever we have (dependents defaults to none).
        is_new_question = slot not in session.asked_slots
        if is_new_question and session.questions_asked >= MAX_QUESTIONS:
            return Decision(
                state_before=state_before,
                state_after="COMPUTING",
                next_action="COMPUTE",
                reason=(
                    f"question budget exhausted ({budget}); forcing compute "
                    f"with available facts instead of asking for '{slot}'"
                ),
            )
        return Decision(
            state_before=state_before,
            state_after=ask_state,
            next_action="ASK",
            reason=f"slot '{slot}' empty; budget {budget}",
        )

    # 3) All required slots filled. Confirm before compute, unless already confirmed.
    if not session.slots.confirmed:
        return Decision(
            state_before=state_before,
            state_after="CONFIRM_FACTS",
            next_action="CONFIRM",
            reason=f"all required slots filled; confirming before compute (budget {budget})",
        )

    # 4) Confirmed -> compute.
    return Decision(
        state_before=state_before,
        state_after="COMPUTING",
        next_action="COMPUTE",
        reason=f"facts confirmed; computing the return (budget {budget})",
    )


__all__ = [
    "next_action",
    "first_unfilled_required_slot",
    "MAX_QUESTIONS",
    "MAX_REPAIRS_PER_SLOT",
    "STATES",
    "VALID_FILING_STATUSES",
]
