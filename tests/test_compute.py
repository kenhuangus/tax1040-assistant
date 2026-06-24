"""
Golden-case tests for the deterministic Form 1040 core (compute_1040).

Hand-checked figures per docs/architecture.md §4 (IRS Tax Table midpoint rule,
whole-dollar half-up rounding, TY2025 constants from taxconstants_2025.py).
"""
from __future__ import annotations

import math

import pytest

from app.compute import bracket_tax, compute_1040, round_half_up, tax
from app.models import Dependent, TaxFacts


# --------------------------------------------------------------------------- #
# CORE profile — single / mfj / mfs, no dependents (credits must be 0).
# --------------------------------------------------------------------------- #
def test_single_40k_golden():
    """The frozen contract golden: single wages=40000, withheld=3000."""
    r = compute_1040(
        TaxFacts(filing_status="single", wages=40000, fed_withholding=3000)
    )
    assert r.line_1a == 40000
    assert r.line_1z == 40000
    assert r.line_9 == 40000
    assert r.line_11 == 40000          # AGI
    assert r.line_12 == 15750          # standard deduction
    assert r.line_15 == 24250          # taxable income
    assert r.line_16 == 2675           # tax (midpoint of $24,250 row → 24,275)
    assert r.line_18 == 2675
    assert r.line_19 == 0              # no dependents → no CTC/ODC
    assert r.line_24 == 2675           # total tax
    assert r.line_25d == 3000
    assert r.line_33 == 3000           # total payments
    assert r.line_34 == 325            # refund
    assert r.line_37 == 0
    assert r.refund == 325
    assert r.owed == 0
    assert r.refund == r.line_34
    assert r.owed == r.line_37


def test_single_40k_credits_are_zero():
    """CORE profile must carry zero credits (refundable + nonrefundable)."""
    r = compute_1040(
        TaxFacts(filing_status="single", wages=40000, fed_withholding=3000)
    )
    assert r.line_19 == 0
    assert r.line_27 == 0   # EITC: single, no kids, $40k earned → fully phased out
    assert r.line_28 == 0
    assert r.line_32 == 0


def test_mfj_40k():
    """MFJ wages=40000: taxable 8,500, tax ~850 (midpoint row → 8,525 @ 10%)."""
    r = compute_1040(
        TaxFacts(filing_status="mfj", wages=40000, fed_withholding=3000)
    )
    assert r.line_12 == 31500
    assert r.line_15 == 8500
    assert r.line_16 == 853            # 8,525 * 0.10 = 852.50 → half-up 853
    assert r.line_24 == 853
    assert r.line_33 == 3000
    assert r.refund == 3000 - 853      # 2147
    assert r.owed == 0
    assert r.line_19 == 0
    assert r.line_27 == 0
    assert r.line_28 == 0


def test_mfs_40k_owes():
    """MFS wages=40000, low withholding → owes. MFS gets EITC=0 by rule."""
    r = compute_1040(
        TaxFacts(filing_status="mfs", wages=40000, fed_withholding=1000)
    )
    assert r.line_12 == 15750
    assert r.line_15 == 24250
    assert r.line_16 == 2675           # same brackets as single at this income
    assert r.line_24 == 2675
    assert r.line_27 == 0              # MFS ineligible for EITC in scope
    assert r.line_33 == 1000
    assert r.owed == 2675 - 1000       # 1675
    assert r.refund == 0


# --------------------------------------------------------------------------- #
# Dependents / credits — HoH with one qualifying child.
# --------------------------------------------------------------------------- #
def test_hoh_one_dependent():
    """HoH, 1 qualifying child (under 17, has SSN), wages=40000, withheld=3000.

    line_15 = 40000 - 23625 = 16375 ; midpoint row -> 16375 @ 10% = 1637.50 -> 1638.
    CTC (2200) limited to tax 1638 (line_19); leftover 562 -> ACTC (line_28).
    EITC (1 child, single column): max 4328 phased out at .1598 over 23350.
    """
    dep = Dependent(name="Kid", ssn="111-22-3333", relationship="daughter",
                    is_under_17=True, has_ssn=True)
    r = compute_1040(
        TaxFacts(filing_status="hoh", wages=40000, fed_withholding=3000,
                 dependents=[dep])
    )
    assert r.line_12 == 23625
    assert r.line_15 == 16375
    assert r.line_16 == 1638           # (16375//50*50+25)=16375 ; *0.10=1637.50 -> 1638
    assert r.line_18 == 1638
    assert r.line_19 == 1638           # CTC capped to tax
    assert r.line_22 == 0
    assert r.line_24 == 0              # total tax wiped out by CTC
    assert r.line_28 == 562            # refundable Additional CTC (leftover 2200-1638)
    assert r.line_27 == 1667           # EITC: 4328 - (40000-23350)*0.1598 = 1667.33 -> 1667
    assert r.line_32 == 1667 + 562     # 2229
    assert r.line_33 == 3000 + 2229    # 5229
    assert r.refund == 5229
    assert r.owed == 0


def test_other_dependent_uses_odc_not_ctc():
    """A 17+ dependent (or no SSN) gets the $500 ODC, never the CTC/ACTC."""
    dep = Dependent(name="College Kid", relationship="son",
                    is_under_17=False, has_ssn=True)
    r = compute_1040(
        TaxFacts(filing_status="single", wages=40000, fed_withholding=3000,
                 dependents=[dep])
    )
    # tax 2675; ODC 500 nonrefundable, fully usable; no refundable ACTC.
    assert r.line_19 == 500
    assert r.line_24 == 2675 - 500     # 2175
    assert r.line_28 == 0
    assert r.refund == 3000 - 2175     # 825
    assert r.owed == 0


# --------------------------------------------------------------------------- #
# Zero-tax / edge cases.
# --------------------------------------------------------------------------- #
def test_zero_everything():
    """No wages, no withholding -> all zero, no EITC, refund==owed==0."""
    r = compute_1040(
        TaxFacts(filing_status="single", wages=0, fed_withholding=0)
    )
    assert r.line_15 == 0
    assert r.line_16 == 0
    assert r.line_24 == 0
    assert r.line_27 == 0
    assert r.refund == 0
    assert r.owed == 0


def test_below_standard_deduction_zero_tax():
    """Wages under the standard deduction -> taxable 0, tax 0."""
    r = compute_1040(
        TaxFacts(filing_status="single", wages=10000, fed_withholding=500)
    )
    assert r.line_15 == 0
    assert r.line_16 == 0
    assert r.line_24 == 0
    # withholding fully refunded (plus any EITC).
    assert r.refund >= 500
    assert r.owed == 0


# --------------------------------------------------------------------------- #
# Invariants that must hold for every result.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,wages,wh",
    [
        ("single", 40000, 3000),
        ("single", 40000, 1000),
        ("mfj", 40000, 3000),
        ("mfs", 40000, 1000),
        ("hoh", 80000, 9000),
        ("qss", 120000, 10000),
        ("single", 0, 0),
        ("single", 250000, 40000),
    ],
)
def test_refund_owed_mutually_exclusive(status, wages, wh):
    r = compute_1040(
        TaxFacts(filing_status=status, wages=wages, fed_withholding=wh)
    )
    # Exactly one of refund / owed is nonzero (or both zero when they net).
    assert not (r.refund > 0 and r.owed > 0)
    assert r.refund == r.line_34
    assert r.owed == r.line_37
    # line_33 - line_24 == refund - owed (accounting identity).
    assert r.line_33 - r.line_24 == r.refund - r.owed


# --------------------------------------------------------------------------- #
# Unit checks on the tax-table / rounding primitives.
# --------------------------------------------------------------------------- #
def test_round_half_up():
    assert round_half_up(2674.50) == 2675
    assert round_half_up(852.50) == 853
    assert round_half_up(0.5) == 1
    assert round_half_up(1.49) == 1


def test_tax_table_midpoint_rule():
    # $24,250 taxable -> row midpoint 24,275, single brackets.
    assert tax(24250, "single") == 2675
    # Zero / negative -> 0.
    assert tax(0, "single") == 0
    assert tax(-5, "single") == 0


def test_high_income_no_midpoint():
    # At/above $100k the exact figure is used (no $50 midpoint bump).
    exact = round_half_up(bracket_tax(100000, "single"))
    assert tax(100000, "single") == exact
