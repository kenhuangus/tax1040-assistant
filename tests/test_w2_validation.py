"""
Offline validation/recovery suite for the W-2 extraction tool (``app.w2``).

GOAL: prove that a W-2 with an unreadable/missing Box 1 NEVER silently becomes a
$0-wage return, that legitimate $0 Box 2 withholding is accepted, that messy
real-world money/SSN strings are recovered, and that each rejection carries a
SPECIFIC, user-facing message naming exactly what's wrong so the assistant can
ask for that one piece — and a corrected re-paste then parses.

OFFLINE: every test exercises the deterministic helpers (``_build_w2``,
``_coerce_money``, ``_coerce_ssn``, ``_try_json``) or the JSON fast-path in
``parse_w2`` (which never calls the LLM). The one LLM-path test monkeypatches
``app.llm.llm_turn`` so no network/API key is needed. We assert the LLM path and
the JSON fast-path route through the SAME validation (``_build_w2``).

Owned file: this is the ONLY file this agent writes.
"""
from __future__ import annotations

import json

import pytest

from app.models import ToolCall, W2
from app.w2 import (
    _build_w2,
    _coerce_money,
    _coerce_ssn,
    _try_json,
    parse_w2,
)


# ---------------------------------------------------------------------------
# Happy path — full W-2 via dict, JSON text, and the NovaTax alias.
# ---------------------------------------------------------------------------
def test_build_w2_full_dict():
    w2 = _build_w2(
        {
            "employee_name": "Jane Q Public",
            "ssn": "123-45-6789",
            "wages": 40000,
            "fed_withholding": 3500,
            "employer": "Acme Co",
        }
    )
    assert isinstance(w2, W2)
    assert w2.wages == 40000
    assert w2.fed_withholding == 3500
    assert w2.employee_name == "Jane Q Public"
    assert w2.ssn == "123-45-6789"


def test_parse_w2_full_json_text():
    """Pasted JSON text routes through the deterministic fast-path (no LLM)."""
    raw = json.dumps(
        {
            "employee_name": "John Doe",
            "ssn": "987-65-4321",
            "wages": 52000,
            "fed_withholding": 4100,
        }
    )
    w2 = parse_w2(raw_text=raw)
    assert w2.wages == 52000
    assert w2.fed_withholding == 4100
    assert w2.employee_name == "John Doe"
    assert w2.ssn == "987-65-4321"


def test_parse_w2_json_embedded_in_prose():
    raw = 'Here is my W-2: {"employee_name":"Amy Lin","ssn":"111223333","wages":30000,"fed_withholding":2000} thanks'
    w2 = parse_w2(raw_text=raw)
    assert w2.wages == 30000
    assert w2.ssn == "111-22-3333"


def test_federaltaxwithheld_alias_maps_to_fed_withholding():
    """The NovaTax sample uses ``federalTaxWithheld``; it must map to fed_withholding."""
    raw = json.dumps(
        {
            "employee_name": "Sam Roe",
            "ssn": "123-45-6789",
            "wages": 60000,
            "federalTaxWithheld": 5000,
        }
    )
    w2 = parse_w2(raw_text=raw)
    assert w2.fed_withholding == 5000
    assert w2.wages == 60000


def test_federaltaxwithheld_alias_direct_build():
    w2 = _build_w2(
        {
            "employee_name": "Sam Roe",
            "ssn": "123-45-6789",
            "wages": 60000,
            "federalTaxWithheld": 5000,
        }
    )
    assert w2.fed_withholding == 5000


# ---------------------------------------------------------------------------
# Box 1 wages — missing / zero / unparseable must NOT yield wages=0.
# ---------------------------------------------------------------------------
def _base(**overrides) -> dict:
    d = {
        "employee_name": "Jane Public",
        "ssn": "123-45-6789",
        "wages": 40000,
        "fed_withholding": 3000,
    }
    d.update(overrides)
    return d


def test_missing_wages_rejected_names_box1():
    d = _base()
    del d["wages"]
    with pytest.raises(ValueError) as ei:
        _build_w2(d)
    msg = str(ei.value).lower()
    assert "box 1" in msg or "wages" in msg


def test_zero_wages_rejected_and_never_returns_zero():
    """A $0-wage W-2 means Box 1 was unreadable for this tool — reject, don't accept."""
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(wages=0))
    msg = str(ei.value).lower()
    assert "box 1" in msg or "wages" in msg


def test_zero_wages_string_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(wages="0"))
    assert "box 1" in str(ei.value).lower() or "wages" in str(ei.value).lower()


def test_empty_string_wages_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(wages="   "))
    assert "box 1" in str(ei.value).lower() or "wages" in str(ei.value).lower()


def test_unparseable_wages_rejected_names_box1():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(wages="abc"))
    assert "box 1" in str(ei.value).lower() or "wages" in str(ei.value).lower()


def test_negative_wages_rejected():
    with pytest.raises(ValueError):
        _build_w2(_base(wages=-100))


def test_parse_w2_zero_wages_json_does_not_yield_zero():
    """End-to-end via the JSON fast-path: 0 wages must raise, not produce wages=0."""
    raw = json.dumps(_base(wages=0))
    with pytest.raises(ValueError) as ei:
        parse_w2(raw_text=raw)
    assert "box 1" in str(ei.value).lower() or "wages" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Box 2 federal withholding — 0 is LEGITIMATE; only unparseable/negative fail.
# ---------------------------------------------------------------------------
def test_fed_withholding_zero_is_accepted():
    w2 = _build_w2(_base(fed_withholding=0))
    assert w2.fed_withholding == 0
    assert w2.wages == 40000


def test_fed_withholding_zero_string_accepted():
    w2 = _build_w2(_base(fed_withholding="0"))
    assert w2.fed_withholding == 0


def test_fed_withholding_absent_defaults_to_zero():
    d = _base()
    del d["fed_withholding"]
    w2 = _build_w2(d)
    assert w2.fed_withholding == 0


def test_fed_withholding_unparseable_rejected_names_box2():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(fed_withholding="not money"))
    assert "box 2" in str(ei.value).lower()


def test_fed_withholding_negative_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(fed_withholding=-5))
    assert "box 2" in str(ei.value).lower() or "negative" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Identity — missing name / missing-or-invalid SSN get specific messages.
# ---------------------------------------------------------------------------
def test_missing_name_rejected_names_the_name():
    d = _base()
    del d["employee_name"]
    with pytest.raises(ValueError) as ei:
        _build_w2(d)
    assert "name" in str(ei.value).lower()


def test_blank_name_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(employee_name="   "))
    assert "name" in str(ei.value).lower()


def test_missing_ssn_rejected_names_the_ssn():
    d = _base()
    del d["ssn"]
    with pytest.raises(ValueError) as ei:
        _build_w2(d)
    msg = str(ei.value).lower()
    assert "ssn" in msg or "social security" in msg


def test_invalid_ssn_too_few_digits_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2(_base(ssn="12-345"))
    msg = str(ei.value).lower()
    assert "ssn" in msg or "social security" in msg


# ---------------------------------------------------------------------------
# Messy real-world money + SSN recovery.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["$40,000.00", "40,000", "40000", " 40000 ", "$40000", "40,000.00", 40000, 40000.0],
)
def test_coerce_money_messy_strings(raw):
    assert _coerce_money(raw) == 40000


def test_coerce_money_rounds_half_up_like():
    # round() banker's rounding is fine here; the tool rounds to nearest dollar.
    assert _coerce_money("40000.49") == 40000
    assert _coerce_money("40000.50") in (40000, 40001)  # nearest-dollar; either acceptable


def test_build_w2_with_messy_money_strings():
    w2 = _build_w2(_base(wages="$40,000.00", fed_withholding="3,500"))
    assert w2.wages == 40000
    assert w2.fed_withholding == 3500


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123-45-6789", "123-45-6789"),
        ("123456789", "123-45-6789"),
        ("123 45 6789", "123-45-6789"),
        (" 123-45-6789 ", "123-45-6789"),
        ("123.45.6789", "123-45-6789"),
    ],
)
def test_coerce_ssn_accepts_variants(raw, expected):
    assert _coerce_ssn(raw) == expected


@pytest.mark.parametrize("bad", ["", "  ", "abc", "12-34", "12345", "not-an-ssn"])
def test_coerce_ssn_rejects_non_nine_digit(bad):
    with pytest.raises(ValueError):
        _coerce_ssn(bad)


def test_build_w2_ssn_without_dashes_normalized():
    w2 = _build_w2(_base(ssn="123456789"))
    assert w2.ssn == "123-45-6789"


# ---------------------------------------------------------------------------
# Not-a-W-2 — arbitrary JSON gets the friendly "doesn't look like a W-2" message.
# ---------------------------------------------------------------------------
def test_not_a_w2_dict_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2({"foo": "bar"})
    assert "w-2" in str(ei.value).lower()


def test_not_a_w2_empty_dict_rejected():
    with pytest.raises(ValueError) as ei:
        _build_w2({})
    assert "w-2" in str(ei.value).lower()


def test_parse_w2_not_a_w2_json_rejected():
    with pytest.raises(ValueError) as ei:
        parse_w2(raw_text='{"foo":"bar"}')
    assert "w-2" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# _try_json behavior (deterministic fast-path detector).
# ---------------------------------------------------------------------------
def test_try_json_direct_object():
    assert _try_json('{"a": 1}') == {"a": 1}


def test_try_json_embedded_object():
    assert _try_json('noise {"a": 1} more') == {"a": 1}


def test_try_json_non_json_returns_none():
    assert _try_json("just some prose with no json") is None


# ---------------------------------------------------------------------------
# Recovery: a rejection naming the missing field, then a corrected re-paste
# parses successfully (same validation path).
# ---------------------------------------------------------------------------
def test_recovery_after_zero_wages_repaste():
    bad = json.dumps(_base(wages=0))
    with pytest.raises(ValueError):
        parse_w2(raw_text=bad)
    # User re-pastes with a real Box 1 amount.
    good = json.dumps(_base(wages=40000))
    w2 = parse_w2(raw_text=good)
    assert w2.wages == 40000


def test_recovery_after_missing_ssn_repaste():
    d = _base()
    del d["ssn"]
    with pytest.raises(ValueError):
        parse_w2(raw_text=json.dumps(d))
    d["ssn"] = "123-45-6789"
    w2 = parse_w2(raw_text=json.dumps(d))
    assert w2.ssn == "123-45-6789"


# ---------------------------------------------------------------------------
# LLM path routes through the SAME validation (offline via monkeypatch).
# ---------------------------------------------------------------------------
def _fake_llm_turn_factory(emit_args: dict):
    def _fake(system, messages, tools):  # signature matches app.llm.llm_turn
        tc = ToolCall(name="emit_w2", args=dict(emit_args), result=None, ok=True)
        return [tc], ""
    return _fake


def test_llm_path_valid(monkeypatch):
    import app.llm as llm

    monkeypatch.setattr(
        llm,
        "llm_turn",
        _fake_llm_turn_factory(
            {
                "employee_name": "Pat Kim",
                "ssn": "222-33-4444",
                "wages": 71000,
                "fed_withholding": 6000,
            }
        ),
    )
    # Non-JSON text forces the LLM branch (the JSON fast-path won't match).
    w2 = parse_w2(raw_text="My W-2 wages were seventy-one thousand dollars.")
    assert w2.wages == 71000
    assert w2.ssn == "222-33-4444"


def test_llm_path_omitted_wages_rejected(monkeypatch):
    """Model OMITS Box 1 (can't read it) -> same missing-wages rejection."""
    import app.llm as llm

    monkeypatch.setattr(
        llm,
        "llm_turn",
        _fake_llm_turn_factory(
            {
                "employee_name": "Pat Kim",
                "ssn": "222-33-4444",
                # wages omitted entirely (the new prompt tells the model to omit
                # boxes it can't read rather than emit 0)
                "fed_withholding": 6000,
            }
        ),
    )
    with pytest.raises(ValueError) as ei:
        parse_w2(raw_text="An unreadable W-2 image description.")
    assert "box 1" in str(ei.value).lower() or "wages" in str(ei.value).lower()


def test_llm_path_zero_wages_rejected(monkeypatch):
    """Even if the model still emits 0, validation rejects it (defense in depth)."""
    import app.llm as llm

    monkeypatch.setattr(
        llm,
        "llm_turn",
        _fake_llm_turn_factory(
            {
                "employee_name": "Pat Kim",
                "ssn": "222-33-4444",
                "wages": 0,
                "fed_withholding": 0,
            }
        ),
    )
    with pytest.raises(ValueError) as ei:
        parse_w2(raw_text="Some prose that triggers the LLM branch.")
    assert "box 1" in str(ei.value).lower() or "wages" in str(ei.value).lower()
