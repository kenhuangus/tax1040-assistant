"""
Offline tests for the Observation display projection (app.trace.display_events).

These run WITHOUT a server or API key: we hand-build a list of Events covering a
W-2 parse turn, an off-topic refusal turn (scope guardrail hit), and a compute
turn, then assert the projection surfaces everything the judge-facing UI needs
for the four pillars — and that a full SSN is NEVER present (only XXX-XX-####).
"""
from __future__ import annotations

from app.models import Decision, Event, GuardrailHit, ToolCall
from app.trace import display_events, event_to_display


class _FakeSession:
    """Minimal stand-in for app.session.Session (display_events only reads
    .id, .events, .questions_asked)."""

    def __init__(self, events, questions_asked=0, sid="sess-test"):
        self.id = sid
        self.events = events
        self.questions_asked = questions_asked


def _sample_events():
    # Turn 1: W-2 parse + set_slot. user_msg carries a FULL SSN that must be
    # redacted; tool result echoes the SSN too.
    t1 = Event(
        turn=1,
        user_msg="Here is my W-2. Employee Jordan Rivera SSN 123-45-6789, wages 40000.",
        tool_calls=[
            ToolCall(name="parse_w2", args={"image_b64": "..."}, ok=True,
                     result={"employee_name": "Jordan Rivera", "ssn": "123-45-6789"}),
            ToolCall(name="set_slot", args={"slot": "w2", "ssn": "123-45-6789"}, ok=True),
        ],
        slot_changes={"w2": {"ssn": "123-45-6789", "wages": 40000}},
        guardrail_hits=[],
        decision=Decision(state_before="AWAIT_W2", state_after="ASK_FILING_STATUS",
                          next_action="ASK", reason="slot 'filing_status' empty; budget 1/5"),
        latency_ms=812,
    )
    # Turn 2: off-topic refusal -> scope guardrail hit, REFUSE, no question.
    t2 = Event(
        turn=2,
        user_msg="What's the weather and give me tax advice?",
        tool_calls=[],
        slot_changes={},
        guardrail_hits=[GuardrailHit(rule="scope", detail="off-topic or advice-seeking input")],
        decision=Decision(state_before="ASK_FILING_STATUS", state_after="ASK_FILING_STATUS",
                          next_action="REFUSE", reason="scope gate: off-topic/advice; refused without consuming a question"),
        latency_ms=2,
    )
    # Turn 3: compute the return.
    t3 = Event(
        turn=3,
        user_msg="yes please go ahead",
        tool_calls=[ToolCall(name="compute_1040", args={}, ok=True, result={"refund": 325})],
        slot_changes={"confirmed": True},
        guardrail_hits=[],
        decision=Decision(state_before="CONFIRM_FACTS", state_after="COMPUTING",
                          next_action="COMPUTE", reason="facts confirmed; computing the return (budget 1/5)"),
        latency_ms=1500,
    )
    return [t1, t2, t3]


def test_display_surfaces_four_pillars():
    sess = _FakeSession(_sample_events(), questions_asked=1)
    proj = display_events(sess)
    assert len(proj) == 3

    p1, p2, p3 = proj

    # --- Pillar 1 (Chat loop): decision action + reason + state transition + budget.
    assert p1["decision"]["next_action"] == "ASK"
    assert "filing_status" in p1["decision"]["reason"]
    assert p1["decision"]["state_before"] == "AWAIT_W2"
    assert p1["decision"]["state_after"] == "ASK_FILING_STATUS"
    assert p1["question_budget"].endswith("/5")

    # --- Pillar 2 (Tools): tool names + ok flag, per call and rolled up.
    assert p1["tools_summary"]["names"] == ["parse_w2", "set_slot"]
    assert p1["tools_summary"]["ok"] is True
    assert {tc["name"] for tc in p1["tool_calls"]} == {"parse_w2", "set_slot"}
    assert p3["tools_summary"]["names"] == ["compute_1040"]

    # --- Pillar 3 (Guardrails): scope hit + refusal flagged on the refusal turn.
    assert p2["is_refusal"] is True
    assert p2["decision"]["next_action"] == "REFUSE"
    assert any(h["rule"] == "scope" for h in p2["guardrail_hits"])
    # Non-refusal turns are not flagged.
    assert p1["is_refusal"] is False
    assert p3["is_refusal"] is False

    # --- Pillar 4 (Observation): a SHA-256 audit hash is present per turn.
    for p in proj:
        assert isinstance(p["audit_hash"], str) and len(p["audit_hash"]) == 64
        int(p["audit_hash"], 16)  # must be valid hex


def test_full_ssn_never_appears_only_masked():
    sess = _FakeSession(_sample_events(), questions_asked=1)
    proj = display_events(sess)
    blob = repr(proj)  # serialize the ENTIRE projection (strings, nested dicts, lists)

    # The full SSN must NEVER leak anywhere in the display projection.
    assert "123-45-6789" not in blob
    assert "123456789" not in blob
    # The masked form (last 4 kept) is what should be present instead.
    assert "XXX-XX-6789" in blob

    # Spot-check the specific surfaces that carried the raw SSN.
    p1 = proj[0]
    assert "123-45-6789" not in p1["user_msg"]
    assert "XXX-XX-6789" in p1["user_msg"]
    assert "123-45-6789" not in repr(p1["slot_changes"])
    assert "123-45-6789" not in repr(p1["tool_calls"])


def test_budget_is_monotonic_and_capped():
    # Two ASK turns then a refusal: running budget should climb on ASKs, never on
    # the refusal, and never exceed the session's final questions_asked.
    evs = _sample_events()
    sess = _FakeSession(evs, questions_asked=1)
    proj = display_events(sess)
    budgets = [p["question_budget"] for p in proj]
    used = [int(b.split("/")[0]) for b in budgets]
    assert used == sorted(used)            # monotonic non-decreasing
    assert max(used) <= 1                  # capped at final questions_asked
    assert all(b.endswith("/5") for b in budgets)


def test_empty_session_returns_empty_list():
    assert display_events(_FakeSession([], questions_asked=0)) == []


def test_event_to_display_handles_missing_decision():
    ev = Event(turn=1, user_msg="hello", tool_calls=[], guardrail_hits=[], decision=None)
    out = event_to_display(ev, session_id="s", question_budget="0/5")
    assert out["decision"] is None
    assert out["is_refusal"] is False
    assert out["tools_summary"]["count"] == 0
    assert len(out["audit_hash"]) == 64
