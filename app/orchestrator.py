"""
P1 — The harness brain: ``advance(session, user_msg)``.

One turn of the conversation. The flow (architecture §2) is owned by CODE; the
model only phrases tone and decides which tools to call:

  1. ``guardrails.scope_check`` BEFORE any LLM content call. Off-topic / advice
     -> fixed refusal, emit a guardrail_hit, record the event, return.
     **No LLM content call, no question counted.**
  2. ``llm.llm_turn`` in tool-use mode. We read ONLY ``tool_use`` blocks.
     Numbers in prose ``text`` are IGNORED (P2 invariant).
  3. Each tool_use -> ``tools.dispatch`` (the ONLY state-mutating path).
  4. ``statemachine.next_action`` computes the next action from filled+valid slots.
  5. BUDGET GATE: ASK for a *new* required slot counts a question; at the cap
     (>=5) we force COMPUTE instead (statemachine returns COMPUTE in that case).
     Confirmations / repairs / refusals / same-slot re-prompts never count.
  6. If the decision is COMPUTE, the harness itself dispatches compute_1040 (the
     gate is code-owned, not left to the model). A follow-up tool-result turn
     lets the model phrase the result using ONLY tool-computed values.
  7. Append the turn Event (incl. the Decision) to the log (P4) and return.

Return shape (contract): ``{reply, state, questions_asked, can_download}``.
"""
from __future__ import annotations

import time
from typing import Optional

from app.guardrails import scope_check
from app.llm import SYSTEM_PROMPT, llm_turn
from app.models import Event, GuardrailHit, ToolCall
from app.statemachine import MAX_QUESTIONS, first_unfilled_required_slot, next_action
from app.tools import TOOLS, dispatch
from app.trace import build_event, record_event

# How many model<->tool round-trips we allow within a single user turn. Bounds
# the tool-use loop so a misbehaving model can't spin forever. 4 is plenty:
# parse_w2 -> set_slot -> compute -> fill in one user turn at most.
_MAX_TOOL_ROUNDS = 4


def _slot_changes_merge(dst: dict, src: dict) -> None:
    for k, v in (src or {}).items():
        dst[k] = v


def _content_to_message(assistant_text: str, tool_calls: list[ToolCall]) -> dict:
    """Reconstruct the assistant turn (text + tool_use) for the message history.

    We rebuild from our parsed pieces (rather than storing raw SDK blocks) so the
    history stays plain dicts that re-serialize cleanly on the next call.
    """
    content: list[dict] = []
    if assistant_text:
        content.append({"type": "text", "text": assistant_text})
    for tc in tool_calls:
        tid = (tc.args or {}).get("_tool_use_id") or f"tu_{tc.name}"
        inp = {k: v for k, v in (tc.args or {}).items() if k != "_tool_use_id"}
        content.append({"type": "tool_use", "id": tid, "name": tc.name, "input": inp})
    return {"role": "assistant", "content": content}


def _tool_results_message(tool_calls: list[ToolCall], results: list[dict]) -> dict:
    """Build the user-role tool_result message answering each tool_use."""
    blocks: list[dict] = []
    for tc, res in zip(tool_calls, results):
        tid = (tc.args or {}).get("_tool_use_id") or f"tu_{tc.name}"
        payload = {k: v for k, v in res.items() if k not in ("guardrail", "slot_changes")}
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": _json_safe(payload),
                "is_error": not res.get("ok", True),
            }
        )
    return {"role": "user", "content": blocks}


def _json_safe(payload: dict) -> str:
    import json

    return json.dumps(payload, default=str)


def advance(session, user_msg: str) -> dict:
    """Advance the conversation by one user turn. See module docstring for flow."""
    started = time.time()
    turn_index = len(session.events) + 1
    state_before = session.state

    # ---- 1) Scope gate (pre-LLM, deterministic). ------------------------------
    scope = scope_check(user_msg, session.state)
    if not scope.ok:
        hit = GuardrailHit(rule=scope.rule, detail="off-topic or advice-seeking input", slot="")
        decision = _decision_no_count(
            state_before,
            next_action="REFUSE",
            reason="scope gate: off-topic/advice; refused without consuming a question",
        )
        event = build_event(
            turn=turn_index,
            user_msg=user_msg,
            tool_calls=[],
            slot_changes={},
            guardrail_hits=[hit],
            decision=decision,
            latency_ms=_ms(started),
        )
        record_event(session, event)
        # State and counter UNCHANGED (refusal does not advance the machine).
        return _reply(session, scope.refusal or "")

    # ---- 2-3) LLM tool-use loop; dispatch ONLY tool_use blocks. ---------------
    session.history.append({"role": "user", "content": user_msg})

    all_tool_calls: list[ToolCall] = []
    all_guardrail_hits: list[GuardrailHit] = []
    merged_slot_changes: dict = {}
    assistant_text = ""

    rounds = 0
    while rounds < _MAX_TOOL_ROUNDS:
        rounds += 1
        try:
            tool_calls, assistant_text = llm_turn(SYSTEM_PROMPT, session.history, TOOLS)
        except Exception as exc:  # missing key / API error -> graceful degrade
            return _llm_error_turn(session, user_msg, turn_index, started, exc)

        # Record the assistant turn (prose + any tool_use) into history.
        session.history.append(_content_to_message(assistant_text, tool_calls))

        if not tool_calls:
            # PROSE-ONLY TURN: the model spoke but called no tool. Per P2 this
            # changes NOTHING — we do not parse numbers from prose. Break and let
            # the state machine decide the next action from the (unchanged) slots.
            break

        # Dispatch every tool call through the single mutating path.
        results: list[dict] = []
        for tc in tool_calls:
            res = dispatch(tc, session)
            results.append(res)
            all_tool_calls.append(tc)
            _slot_changes_merge(merged_slot_changes, res.get("slot_changes", {}))
            g = getattr(tc, "_guardrail", None)
            if g is not None:
                all_guardrail_hits.append(g)

        # Feed tool results back so the model can phrase using computed values.
        session.history.append(_tool_results_message(tool_calls, results))
        # Loop again: the model may chain (e.g. set_slot then ask the next Q).

    # ---- 4) State machine decision. -------------------------------------------
    decision = next_action(session)

    # ---- 5) Budget gate + question counting. ----------------------------------
    # Count a question ONLY when the decision is ASK for an unfilled required slot
    # we have not already counted. The statemachine already forces COMPUTE at the
    # cap, so if we see ASK here we are below the cap.
    if decision.next_action == "ASK":
        slot = first_unfilled_required_slot(session)
        if slot is not None and slot not in session.asked_slots:
            session.asked_slots.add(slot)
            session.questions_asked += 1

    # ---- 6) If the harness decided to COMPUTE, do it in code (not via prose). --
    if decision.next_action == "COMPUTE" and session.result is None:
        compute_tc = ToolCall(name="compute_1040", args={})
        cres = dispatch(compute_tc, session)
        all_tool_calls.append(compute_tc)
        _slot_changes_merge(merged_slot_changes, cres.get("slot_changes", {}))
        g = getattr(compute_tc, "_guardrail", None)
        if g is not None:
            all_guardrail_hits.append(g)
        if cres.get("ok"):
            # Let the model acknowledge the computed result using ONLY tool values.
            session.history.append(
                _content_to_message("", [compute_tc])
            )
            session.history.append(_tool_results_message([compute_tc], [cres]))
            try:
                _, follow_text = llm_turn(SYSTEM_PROMPT, session.history, TOOLS)
                if follow_text:
                    assistant_text = follow_text
                    session.history.append({"role": "assistant", "content": follow_text})
            except Exception:
                # No key / API issue: fall back to a deterministic summary line.
                assistant_text = _result_summary(session)

    # Advance the session state to where the decision points.
    session.state = decision.state_after

    # ---- 7) Record the turn Event (P4) and return. ----------------------------
    if not assistant_text:
        assistant_text = _fallback_reply(session, decision.next_action)

    event = build_event(
        turn=turn_index,
        user_msg=user_msg,
        tool_calls=all_tool_calls,
        slot_changes=merged_slot_changes,
        guardrail_hits=all_guardrail_hits,
        decision=decision,
        latency_ms=_ms(started),
    )
    record_event(session, event)
    return _reply(session, assistant_text)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


def _reply(session, reply: str) -> dict:
    return {
        "reply": reply,
        "state": session.state,
        "questions_asked": session.questions_asked,
        "can_download": session.result is not None,
    }


def _decision_no_count(state_before: str, *, next_action: str, reason: str):
    from app.models import Decision

    return Decision(
        state_before=state_before,
        state_after=state_before,  # refusal does not move the machine
        next_action=next_action,
        reason=reason,
    )


def _result_summary(session) -> str:
    r = session.result
    if r is None:
        return "Your return is ready."
    if r.refund > 0:
        return f"All done — I've prepared your 1040. You're getting a refund of ${r.refund:,}. You can download it now."
    if r.owed > 0:
        return f"All done — I've prepared your 1040. You owe ${r.owed:,}. You can download it now."
    return "All done — I've prepared your 1040. You can download it now."


def _fallback_reply(session, action: str) -> str:
    """Deterministic phrasing when the model returned no usable text.

    Keeps the app functional without an LLM key for the happy-path states (used
    by the self-test and local no-key runs). Phrasings stay generic and never
    volunteer numbers except tool-computed result values.
    """
    if action == "ASK":
        slot = first_unfilled_required_slot(session)
        if slot == "w2":
            return "To get started, could you paste your W-2 (or upload a photo of it)?"
        if slot == "filing_status":
            return "Got it. How are you filing this year — single, married filing jointly, or head of household?"
        if slot == "dependents":
            return "Thanks! Anyone you support — kids or dependents — I should count, so I can check the Child Tax Credit?"
        return "Could you tell me a bit more so I can continue?"
    if action == "CONFIRM":
        return _confirm_summary(session)
    if action == "COMPUTE":
        return _result_summary(session)
    if action == "REPAIR":
        return "I need to fix one of your earlier answers before we continue — could you clarify?"
    return "Could you tell me a bit more?"


def _confirm_summary(session) -> str:
    s = session.slots
    parts: list[str] = []
    pretty = {
        "single": "single",
        "mfj": "married filing jointly",
        "mfs": "married filing separately",
        "hoh": "head of household",
        "qss": "qualifying surviving spouse",
    }.get(s.filing_status or "", s.filing_status or "")
    if pretty:
        parts.append(pretty)
    if s.w2 is not None:
        parts.append(f"${s.w2.wages:,} wages")
        parts.append(f"${s.w2.fed_withholding:,} withheld")
    dep = "no dependents" if not s.dependents else f"{len(s.dependents)} dependent(s)"
    parts.append(dep)
    return "Here's what I have: " + ", ".join(parts) + ". Want me to go ahead and fill your 1040?"


def _llm_error_turn(session, user_msg: str, turn_index: int, started: float, exc: Exception) -> dict:
    """Graceful path when the LLM cannot be reached (e.g. no API key set)."""
    from app.models import Decision

    detail = f"LLM unavailable: {exc}"
    hit = GuardrailHit(rule="schema", detail=detail, slot="")
    decision = Decision(
        state_before=session.state,
        state_after=session.state,
        next_action="REFUSE",
        reason="LLM call failed; turn aborted without state change",
    )
    event = build_event(
        turn=turn_index,
        user_msg=user_msg,
        tool_calls=[],
        slot_changes={},
        guardrail_hits=[hit],
        decision=decision,
        latency_ms=_ms(started),
    )
    record_event(session, event)
    return _reply(
        session,
        "I'm having trouble reaching the assistant right now. Please try again in a moment.",
    )


__all__ = ["advance"]


# ---------------------------------------------------------------------------
# Self-test: the prose-only-no-tool invariant (P2), runnable WITHOUT an API key.
# Verifies that a turn where the "model" emits prose with NO tool call leaves all
# slots empty and fires no compute. We monkeypatch llm_turn to avoid the network.
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import app.orchestrator as orch
    from app.session import Session

    # --- Test 1: prose-only turn must NOT mutate slots or compute. -----------
    def _fake_prose_only(system, messages, tools):
        # The model claims a number in prose but calls NO tool.
        return [], "Your wages are $40,000 and your refund is $325."

    orig = orch.llm_turn
    orch.llm_turn = _fake_prose_only  # type: ignore[assignment]
    try:
        sess = Session(id="selftest")
        out = orch.advance(sess, "here is my info")
        assert sess.slots.w2 is None, "prose-only turn set the W-2 slot!"
        assert sess.slots.filing_status is None, "prose-only turn set filing_status!"
        assert sess.result is None, "prose-only turn triggered compute!"
        assert out["can_download"] is False, "download unlocked with no result!"
        assert sess.questions_asked == 1 or sess.questions_asked == 0, (
            f"unexpected question count {sess.questions_asked}"
        )
        print("PASS: prose-only-no-tool invariant — slots empty, no compute, no download.")
    finally:
        orch.llm_turn = orig  # type: ignore[assignment]

    # --- Test 2: scope gate refuses without consuming a question or LLM call. -
    sess2 = Session(id="selftest-scope")
    before = sess2.questions_asked

    def _should_not_be_called(*a, **k):  # the LLM must NOT be called on a refusal
        raise AssertionError("LLM was called on an off-topic message!")

    orch.llm_turn = _should_not_be_called  # type: ignore[assignment]
    try:
        out2 = orch.advance(sess2, "give me tax advice on how to avoid taxes")
        assert sess2.questions_asked == before, "scope refusal consumed a question!"
        assert "educational tool, not tax advice" in out2["reply"], "refusal text missing!"
        assert sess2.events[-1].guardrail_hits, "scope hit not recorded in trace!"
        print("PASS: scope gate — refusal, no question consumed, no LLM call, hit recorded.")
    finally:
        orch.llm_turn = orig  # type: ignore[assignment]

    print("orchestrator self-tests passed.")
