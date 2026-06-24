"""
Regression tests for dependent coercion -> CTC vs ODC classification.

Guards the high-severity bug where a qualifying child (under 17, has SSN) was
mis-classified as a $500 Credit for Other Dependents instead of the $2,200
Child Tax Credit, because the model-emitted dependent shape ({"age": 16} or
{"age": "under 17"}) never reached ``Dependent.is_under_17`` / ``has_ssn``.

These exercise ``app.tools._coerce_dependents`` (the real coercion path used by
``set_slot``) and then feed the result through the deterministic compute core.
"""
from __future__ import annotations

import pytest

from app.compute import compute_1040
from app.models import Dependent, TaxFacts
from app.tools import _coerce_dependents


# --------------------------------------------------------------------------- #
# Coercion: model-style child dicts must yield is_under_17=True, has_ssn=True.
# --------------------------------------------------------------------------- #
def test_coerce_child_age_phrase():
    """{"age": "under 17"} -> qualifying child."""
    deps = _coerce_dependents(
        [{"name": "Sam Rivera", "ssn": "111-22-3333", "age": "under 17"}]
    )
    assert len(deps) == 1
    d = deps[0]
    assert isinstance(d, Dependent)
    assert d.name == "Sam Rivera"
    assert d.ssn == "111-22-3333"
    assert d.is_under_17 is True
    assert d.has_ssn is True


def test_coerce_child_age_int():
    """{"age": 16} -> qualifying child."""
    deps = _coerce_dependents([{"name": "Sam", "age": 16, "ssn": "111-22-3333"}])
    assert len(deps) == 1
    d = deps[0]
    assert d.is_under_17 is True
    assert d.has_ssn is True


def test_coerce_child_age_under17_nospace():
    """{"age": "under17"} (no space) -> qualifying child."""
    deps = _coerce_dependents([{"name": "Pat", "ssn": "111-22-3333", "age": "under17"}])
    assert deps[0].is_under_17 is True
    assert deps[0].has_ssn is True


def test_coerce_qualifying_child_flag():
    """An explicit qualifying_child / relationship signal also works."""
    deps = _coerce_dependents(
        [{"name": "Jo", "ssn": "111-22-3333", "relationship": "daughter", "age": 9}]
    )
    assert deps[0].is_under_17 is True
    assert deps[0].has_ssn is True


def test_coerce_explicit_is_under_17_honored():
    """An explicit is_under_17 is taken verbatim and unknown keys are dropped."""
    deps = _coerce_dependents(
        [{"name": "Kid", "ssn": "111-22-3333", "is_under_17": True, "foo": "bar"}]
    )
    assert deps[0].is_under_17 is True
    assert deps[0].has_ssn is True


def test_coerce_adult_dependent_not_under_17():
    """An adult dependent must NOT be a qualifying child."""
    deps = _coerce_dependents([{"name": "Mom", "age": 70, "ssn": "222-33-4444"}])
    assert deps[0].is_under_17 is False
    assert deps[0].has_ssn is True


def test_coerce_has_ssn_false_when_missing():
    """No SSN -> has_ssn defaults False (so an under-17 w/o SSN is ODC, not CTC)."""
    deps = _coerce_dependents([{"name": "Baby", "age": 1}])
    assert deps[0].is_under_17 is True
    assert deps[0].has_ssn is False


def test_coerce_empty_and_none():
    assert _coerce_dependents([]) == []
    assert _coerce_dependents("none") == []
    assert _coerce_dependents(0) == []


def test_coerce_malformed_raises():
    """Truly malformed input still raises ValueError (-> schema guardrail)."""
    with pytest.raises(ValueError):
        _coerce_dependents(123)
    with pytest.raises(ValueError):
        _coerce_dependents("{not json")


# --------------------------------------------------------------------------- #
# End-to-end: coerced child -> compute -> $2,200 CTC and correct refund.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "child_dict",
    [
        {"name": "Sam Rivera", "ssn": "111-22-3333", "age": "under 17"},
        {"name": "Sam", "age": 16, "ssn": "111-22-3333"},
    ],
)
def test_qualifying_child_gets_2200_ctc_and_4312_refund(child_dict):
    """single + 1 qualifying child, wages=40000, withheld=3120:
    line_19 == 2200 (CTC, not the $500 ODC) and refund == 4312 (incl. EITC)."""
    deps = _coerce_dependents([child_dict])
    assert deps[0].is_under_17 is True
    assert deps[0].has_ssn is True

    r = compute_1040(
        TaxFacts(
            filing_status="single",
            wages=40000,
            fed_withholding=3120,
            dependents=deps,
        )
    )
    assert r.line_19 == 2200            # Child Tax Credit (NOT the $500 ODC)
    assert r.line_27 == 1667            # EITC, 1 child, single column
    assert r.refund == 4312
    assert r.owed == 0


def test_adult_dependent_gets_500_odc():
    """An adult dependent (age 70) yields the $500 Credit for Other Dependents."""
    deps = _coerce_dependents([{"name": "Mom", "age": 70, "ssn": "222-33-4444"}])
    r = compute_1040(
        TaxFacts(
            filing_status="single",
            wages=40000,
            fed_withholding=3120,
            dependents=deps,
        )
    )
    assert r.line_19 == 500             # ODC, never the CTC
    assert r.line_28 == 0              # ODC is not refundable
