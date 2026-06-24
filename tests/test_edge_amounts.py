"""
Exhaustive edge-case suite for the deterministic Form 1040 tax core.

GOAL: prove ``app.compute.compute_1040`` produces a valid 2025 Form 1040 for ANY
W-2 wage amount and ANY of the five filing statuses without raising and without
wrong math.

Strategy — independent cross-check, NOT a tautology:
  The reference tax math here is **re-derived from the frozen constants**
  (``STD_DED`` / ``BRACKETS`` / ``CTC`` / ``ODC`` / ``EITC`` / ``ACTC_*`` in
  ``app.taxconstants_2025``). It never calls ``app.compute.tax`` /
  ``bracket_tax`` / ``_eitc`` etc. The only shared dependency is the rounding
  *convention* (IRS whole-dollar, half-up), which we reproduce with our own
  ``_round_half_up`` using ``Decimal(str(x))`` so the comparison is exact rather
  than off-by-one. This makes the test a genuine second implementation of the
  §4 spec compared against the engine.

Owned file: this is the ONLY file this agent writes. It imports the public API
``compute_1040`` and the models, plus the constants module for the reference.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.compute import compute_1040
from app.models import Dependent, Form1040Result, TaxFacts
from app.taxconstants_2025 import (
    ACTC_CAP,
    ACTC_FLOOR,
    ACTC_RATE,
    BRACKETS,
    CTC,
    CTC_PHASEOUT,
    CTC_PHASEOUT_INCREMENT,
    CTC_PHASEOUT_REDUCTION_PER_INCREMENT,
    EITC,
    ODC,
    STD_DED,
)

# --------------------------------------------------------------------------- #
# Parameter space.
# --------------------------------------------------------------------------- #
STATUSES: list[str] = ["single", "mfj", "mfs", "hoh", "qss"]

# Wages: full range incl. every bracket / std-ded / tax-table boundary asked for.
WAGES: list[int] = [
    0,
    1,
    100,
    12_000,
    15_749,        # one below single/mfs std ded
    15_750,        # == single/mfs std ded -> taxable 0
    15_751,        # one above -> taxable 1
    23_850,        # mfj/qss 10% bracket top (also a wage value)
    39_999,
    40_000,        # the canonical golden wage
    40_001,
    99_999,        # last cent under the $100k tax-table cutoff (on taxable)
    100_000,       # tax-table cutoff
    100_001,
    250_525,       # single/mfs 32% bracket top
    626_350,       # single/hoh 35% bracket top
    1_000_000,
    9_999_999,
    50_000_000,
]


# --------------------------------------------------------------------------- #
# Independent reference implementation — re-derived from the constants.
# --------------------------------------------------------------------------- #
def _round_half_up(amount: float | Decimal) -> int:
    """IRS whole-dollar rounding, halves away from zero (up for positives).

    Reproduced independently here; uses ``Decimal(str(x))`` exactly like the
    spec describes so the cross-check is exact, not approximate.
    """
    return int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _bracket_tax_ref(income: float, status: str) -> float:
    """Marginal progressive tax over the status brackets — independent re-derive.

    Walks (upper_limit, rate) bands from the frozen BRACKETS table. Float math,
    matching the spec's ``bracket_tax``; we then round with _round_half_up.
    """
    tax = 0.0
    previous = 0.0
    for upper_limit, rate in BRACKETS[status]:
        if income <= previous:
            break
        band = min(income, upper_limit) - previous
        tax += band * rate
        previous = upper_limit
    return tax


def _tax_ref(taxable: int, status: str) -> int:
    """Form 1040 Line-16 reference.

    taxable <= 0            -> 0
    taxable <  100_000      -> IRS Tax Table: midpoint of the $50 row, (n//50)*50+25
    taxable >= 100_000      -> exact figure (Tax Computation Worksheet)
    Then half-up to whole dollars.
    """
    if taxable <= 0:
        return 0
    if taxable >= 100_000:
        base: float = float(taxable)
    else:
        base = (taxable // 50) * 50 + 25
    return _round_half_up(_bracket_tax_ref(base, status))


def _std_ded_ref(status: str) -> int:
    return STD_DED[status]


def _eitc_column_ref(status: str) -> str:
    return "mfj" if status in ("mfj", "qss") else "single"


def _eitc_ref(wages: int, agi: int, status: str, qc: int) -> int:
    """Earned Income Credit reference (wages-only profile: earned == agi == wages,
    investment income 0). MFS ineligible in scope -> 0. Independent re-derive
    from the EITC table."""
    if wages <= 0:
        return 0
    if status == "mfs":
        return 0
    bucket = {0: "0children", 1: "1child", 2: "2children"}.get(
        min(qc, 3), "3plusChildren"
    )
    band = EITC[bucket][_eitc_column_ref(status)]

    earned = float(wages)
    if earned >= band["phase_in_end"]:
        credit = band["max_credit"]
    else:
        credit = earned * band["phase_in_rate"]

    phase_base = max(earned, float(agi))
    if phase_base > band["phase_out_start"]:
        reduction = (phase_base - band["phase_out_start"]) * band["phase_out_rate"]
        credit = max(0.0, credit - reduction)

    return _round_half_up(credit)


def _ctc_phaseout_reduction_ref(agi: int, status: str) -> int:
    threshold = CTC_PHASEOUT[status]
    if agi <= threshold:
        return 0
    excess = agi - threshold
    increments = -(-excess // CTC_PHASEOUT_INCREMENT)  # ceil
    return increments * CTC_PHASEOUT_REDUCTION_PER_INCREMENT


def reference_1040(facts: TaxFacts) -> dict:
    """A full, independent re-derivation of every line the engine populates,
    following docs/architecture.md §4 step by step. Returns a dict of line ->
    int. Used to cross-check compute_1040 line-for-line."""
    status = facts.filing_status
    wages = int(facts.wages)
    wh = int(facts.fed_withholding)
    deps = list(facts.dependents)

    qc = sum(1 for d in deps if d.is_under_17 and d.has_ssn)
    od = len(deps) - qc

    line_11 = wages  # AGI
    line_12 = _std_ded_ref(status)
    line_15 = max(0, line_11 - line_12)
    line_16 = _tax_ref(line_15, status)
    line_18 = line_16

    ctc_odc_full = qc * CTC + od * ODC
    if ctc_odc_full > 0:
        red = _ctc_phaseout_reduction_ref(line_11, status)
        ctc_odc_after = max(0, ctc_odc_full - red)
    else:
        ctc_odc_after = 0
    line_19 = min(ctc_odc_after, line_18)

    if qc > 0:
        ctc_only_full = min(qc * CTC, max(0, ctc_odc_after - od * ODC))
        ctc_used_nonref = max(0, line_19 - od * ODC)
        leftover_ctc = max(0, ctc_only_full - ctc_used_nonref)
        earned_based = _round_half_up(ACTC_RATE * max(0, wages - ACTC_FLOOR))
        per_child_cap = ACTC_CAP * qc
        line_28 = max(0, min(earned_based, per_child_cap, leftover_ctc))
    else:
        line_28 = 0

    line_21 = line_19
    line_22 = max(0, line_18 - line_21)
    line_24 = line_22
    line_27 = _eitc_ref(wages, line_11, status, qc)
    line_25d = wh
    line_32 = line_27 + line_28
    line_33 = line_25d + line_32

    if line_33 > line_24:
        line_34 = line_33 - line_24
        line_37 = 0
    else:
        line_37 = line_24 - line_33
        line_34 = 0

    return {
        "line_1a": wages,
        "line_1z": wages,
        "line_9": wages,
        "line_11": line_11,
        "line_12": line_12,
        "line_15": line_15,
        "line_16": line_16,
        "line_18": line_18,
        "line_19": line_19,
        "line_22": line_22,
        "line_24": line_24,
        "line_25a": wh,
        "line_25d": line_25d,
        "line_27": line_27,
        "line_28": line_28,
        "line_32": line_32,
        "line_33": line_33,
        "line_34": line_34,
        "line_35a": line_34,
        "line_37": line_37,
        "refund": line_34,
        "owed": line_37,
    }


# Every int money field on Form1040Result, for the non-negative-whole-int check.
_MONEY_FIELDS: list[str] = [f for f in Form1040Result.model_fields if f.startswith(("line_", "refund", "owed"))]


def _withholding_grid(ref_total_tax: int) -> list[int]:
    """0, 1, a value < tax, == tax (when tax>0), > tax (refund), >> tax."""
    grid = [0, 1]
    if ref_total_tax > 0:
        grid.append(max(0, ref_total_tax - 1))   # < tax
        grid.append(ref_total_tax)                # == tax
    grid.append(ref_total_tax + 1)                # > tax (refund)
    grid.append(ref_total_tax + 1_000_000)        # >> tax (large refund)
    # de-dup, keep order
    seen: set[int] = set()
    out: list[int] = []
    for v in grid:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# --------------------------------------------------------------------------- #
# Build the case matrix: (status, wages, withholding, dependents, label).
# Dependents variants: 0, 1/2/3 qualifying kids, and 1 adult dependent.
# --------------------------------------------------------------------------- #
def _kids(n: int) -> list[Dependent]:
    return [
        Dependent(name=f"Kid{i}", ssn="111-22-3333", relationship="child",
                  is_under_17=True, has_ssn=True)
        for i in range(n)
    ]


_ADULT = [Dependent(name="Grandpa", ssn="222-33-4444", relationship="parent",
                    is_under_17=False, has_ssn=True)]

_DEP_VARIANTS: list[tuple[str, list[Dependent]]] = [
    ("0dep", []),
    ("1kid", _kids(1)),
    ("2kids", _kids(2)),
    ("3kids", _kids(3)),
    ("adult", list(_ADULT)),
]


def _all_cases() -> list[tuple]:
    cases: list[tuple] = []
    for status in STATUSES:
        for wages in WAGES:
            for dep_label, deps in _DEP_VARIANTS:
                # Reference total tax (line_24) drives the withholding grid so
                # we deterministically hit <,==,> tax for every income point.
                ref = reference_1040(
                    TaxFacts(filing_status=status, wages=wages,
                             fed_withholding=0, dependents=list(deps))
                )
                for wh in _withholding_grid(ref["line_24"]):
                    label = f"{status}-w{wages}-wh{wh}-{dep_label}"
                    cases.append((status, wages, wh, list(deps), label))
    return cases


_CASES = _all_cases()
_CASE_IDS = [c[-1] for c in _CASES]


# --------------------------------------------------------------------------- #
# THE master invariant test — every combo, every invariant (1,2,3,5,6).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,wages,wh,deps,label", _CASES, ids=_CASE_IDS)
def test_invariants_every_combo(status, wages, wh, deps, label):
    facts = TaxFacts(filing_status=status, wages=wages, fed_withholding=wh,
                     dependents=list(deps))

    # INVARIANT 1: never raises; returns a Form1040Result.
    r = compute_1040(facts)
    assert isinstance(r, Form1040Result), f"{label}: did not return Form1040Result"

    # INVARIANT 2: all money fields are non-negative, whole-number ints.
    for f in _MONEY_FIELDS:
        v = getattr(r, f)
        assert isinstance(v, int) and not isinstance(v, bool), (
            f"{label}: {f}={v!r} is not an int"
        )
        assert v >= 0, f"{label}: {f}={v} is negative"

    # INVARIANT 3: taxable line_15 == max(0, wages - std_ded[status]) for the
    # no-dependent cases (std ded read straight from constants).
    if not deps:
        assert r.line_15 == max(0, wages - STD_DED[status]), (
            f"{label}: line_15 {r.line_15} != max(0, {wages}-{STD_DED[status]})"
        )

    # INVARIANT 6: refund/owed mutually exclusive + accounting identity.
    assert not (r.refund > 0 and r.owed > 0), f"{label}: both refund and owed > 0"
    assert r.refund == r.line_34, f"{label}: refund != line_34"
    assert r.owed == r.line_37, f"{label}: owed != line_37"
    assert r.line_35a == r.line_34, f"{label}: line_35a != line_34"
    assert (r.line_33 - r.line_24) == (r.refund - r.owed), (
        f"{label}: identity (33 - 24) != (refund - owed): "
        f"{r.line_33}-{r.line_24} != {r.refund}-{r.owed}"
    )

    # INVARIANT 5 (+ full line-by-line): every populated line matches the
    # independent reference re-derived from the constants.
    ref = reference_1040(facts)
    for line, expected in ref.items():
        actual = getattr(r, line)
        assert actual == expected, (
            f"{label}: {line} expected {expected}, got {actual}"
        )


# --------------------------------------------------------------------------- #
# INVARIANT 4: line_16 monotonically NON-DECREASING in wages (status fixed),
# and always >= 0. Checked across the full wage ladder for each status, no
# dependents (tax is independent of withholding/dependents at line_16).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", STATUSES)
def test_line16_monotonic_nondecreasing_in_wages(status):
    prev_tax = -1
    prev_wage = None
    for wages in sorted(WAGES):
        r = compute_1040(TaxFacts(filing_status=status, wages=wages,
                                  fed_withholding=0))
        assert r.line_16 >= 0, f"{status} w{wages}: line_16 negative"
        assert r.line_16 >= prev_tax, (
            f"{status}: line_16 DECREASED from {prev_tax} (w{prev_wage}) "
            f"to {r.line_16} (w{wages})"
        )
        prev_tax = r.line_16
        prev_wage = wages


# A denser monotonic sweep right around the $100k tax-table cliff, where the
# midpoint->exact switch is most likely to introduce a non-monotonic jump.
@pytest.mark.parametrize("status", STATUSES)
def test_line16_monotonic_dense_around_100k(status):
    # taxable crosses 100k somewhere in here for every status (std ded < 32k).
    prev_tax = -1
    prev_wage = None
    for wages in range(99_000, 135_000 + 1, 250):
        r = compute_1040(TaxFacts(filing_status=status, wages=wages,
                                  fed_withholding=0))
        assert r.line_16 >= prev_tax, (
            f"{status}: line_16 DECREASED from {prev_tax} (w{prev_wage}) "
            f"to {r.line_16} (w{wages}) near the $100k cliff"
        )
        prev_tax = r.line_16
        prev_wage = wages


# Dense monotonic sweep across the low range too (every $25 step), to catch any
# non-monotonicity from the $50 midpoint table rounding.
@pytest.mark.parametrize("status", STATUSES)
def test_line16_monotonic_dense_low_range(status):
    prev_tax = -1
    prev_wage = None
    for wages in range(0, 60_000 + 1, 25):
        r = compute_1040(TaxFacts(filing_status=status, wages=wages,
                                  fed_withholding=0))
        assert r.line_16 >= prev_tax, (
            f"{status}: line_16 DECREASED from {prev_tax} (w{prev_wage}) "
            f"to {r.line_16} (w{wages})"
        )
        prev_tax = r.line_16
        prev_wage = wages


# --------------------------------------------------------------------------- #
# INVARIANT 5, focused: tax(taxable) equals the midpoint computation below 100k
# and the exact bracket formula at/above 100k. Compared against the engine via
# wages chosen to land taxable on/near the cutoff.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize(
    "taxable",
    [0, 1, 25, 49, 50, 51, 99, 100, 12_345, 49_999, 50_000,
     99_949, 99_950, 99_999, 100_000, 100_001, 250_000, 1_000_000],
)
def test_tax_matches_reference_at_taxable(status, taxable):
    """Drive a precise taxable income through compute by setting
    wages = taxable + std_ded (no deps), then compare line_16 to the independent
    reference tax()."""
    wages = taxable + STD_DED[status]
    r = compute_1040(TaxFacts(filing_status=status, wages=wages,
                              fed_withholding=0))
    assert r.line_15 == taxable, f"{status} taxable={taxable}: line_15 mismatch"
    assert r.line_16 == _tax_ref(taxable, status), (
        f"{status} taxable={taxable}: line_16 {r.line_16} != ref "
        f"{_tax_ref(taxable, status)}"
    )
    # And explicitly verify the two regimes are what the spec says:
    if taxable >= 100_000:
        expected = _round_half_up(_bracket_tax_ref(float(taxable), status))
    elif taxable <= 0:
        expected = 0
    else:
        midpoint = (taxable // 50) * 50 + 25
        expected = _round_half_up(_bracket_tax_ref(midpoint, status))
    assert r.line_16 == expected, (
        f"{status} taxable={taxable}: line_16 {r.line_16} != regime-expected "
        f"{expected}"
    )


# --------------------------------------------------------------------------- #
# INVARIANT 7: CTC / ODC / ACTC / EITC behaviour.
# --------------------------------------------------------------------------- #
def test_one_kid_reduces_tax_by_up_to_2200_capped():
    """A single qualifying child reduces line_19 by up to $2,200, capped at the
    tax (line_18). With ample tax, exactly $2,200 of nonrefundable CTC applies."""
    # High enough income that tax >> 2200, low enough to avoid CTC phaseout.
    facts = TaxFacts(filing_status="single", wages=80_000, fed_withholding=0,
                     dependents=_kids(1))
    r = compute_1040(facts)
    base = compute_1040(TaxFacts(filing_status="single", wages=80_000,
                                 fed_withholding=0))
    assert r.line_18 == base.line_16          # tax before credits unchanged
    assert r.line_19 == CTC                    # full $2,200 nonrefundable CTC
    assert r.line_19 <= r.line_18              # never exceeds the tax
    assert base.line_24 - r.line_24 == CTC     # total tax dropped by exactly 2200


def test_ctc_capped_at_tax_and_actc_kicks_in_low_tax():
    """With low tax, nonrefundable CTC is capped at the tax and the refundable
    Additional CTC (line_28) picks up the remainder (subject to its own caps)."""
    # HoH, 1 kid, wages 40k -> taxable 16,375, tax 1,638; CTC capped to 1638,
    # leftover 562 flows to ACTC (well under earned-based and per-child caps).
    facts = TaxFacts(filing_status="hoh", wages=40_000, fed_withholding=0,
                     dependents=_kids(1))
    r = compute_1040(facts)
    assert r.line_19 == r.line_18 == 1638      # CTC capped exactly at the tax
    assert r.line_24 == 0                       # tax fully wiped
    assert r.line_28 == CTC - 1638             # 562 refundable ACTC
    # ACTC never exceeds its statutory caps.
    assert r.line_28 <= ACTC_CAP * 1
    assert r.line_28 <= _round_half_up(ACTC_RATE * max(0, 40_000 - ACTC_FLOOR))


def test_adult_dependent_gives_500_odc_nonrefundable():
    """An adult dependent yields the $500 ODC, never the CTC, and ODC is not
    refundable (no ACTC)."""
    facts = TaxFacts(filing_status="single", wages=40_000, fed_withholding=0,
                     dependents=list(_ADULT))
    r = compute_1040(facts)
    base = compute_1040(TaxFacts(filing_status="single", wages=40_000,
                                 fed_withholding=0))
    assert r.line_19 == ODC
    assert r.line_28 == 0
    assert base.line_24 - r.line_24 == ODC


@pytest.mark.parametrize("status", ["single", "hoh"])
def test_eitc_positive_for_one_child_at_40k(status):
    """EITC for a single/HoH filer with 1 child at $40k must be > 0 (line_27)."""
    facts = TaxFacts(filing_status=status, wages=40_000, fed_withholding=0,
                     dependents=_kids(1))
    r = compute_1040(facts)
    assert r.line_27 > 0, f"{status}: EITC at $40k/1child should be > 0"
    assert r.line_27 == _eitc_ref(40_000, 40_000, status, 1)


def test_mfs_never_gets_eitc():
    """MFS is EITC-ineligible in scope -> line_27 == 0 even with kids."""
    for n in (0, 1, 2, 3):
        facts = TaxFacts(filing_status="mfs", wages=40_000, fed_withholding=0,
                         dependents=_kids(n))
        r = compute_1040(facts)
        assert r.line_27 == 0, f"mfs {n}kids: EITC should be 0"


def test_three_kids_ctc_up_to_6600_capped_at_tax():
    """3 qualifying children -> up to 3*2200 nonrefundable, capped at tax."""
    facts = TaxFacts(filing_status="mfj", wages=120_000, fed_withholding=0,
                     dependents=_kids(3))
    r = compute_1040(facts)
    base = compute_1040(TaxFacts(filing_status="mfj", wages=120_000,
                                 fed_withholding=0))
    # No phaseout for mfj at 120k (< 400k threshold); tax >> 6600 here.
    assert r.line_19 == 3 * CTC
    assert r.line_19 <= r.line_18
    assert base.line_24 - r.line_24 == 3 * CTC


# --------------------------------------------------------------------------- #
# Sanity: the canonical golden still holds (anchors the whole suite).
# --------------------------------------------------------------------------- #
def test_golden_single_40k_3000():
    r = compute_1040(TaxFacts(filing_status="single", wages=40_000,
                              fed_withholding=3000))
    assert r.line_15 == 24_250
    assert r.line_16 == 2_675
    assert r.refund == 325
    assert r.owed == 0


def test_reference_self_consistency_on_golden():
    """The independent reference itself must reproduce the spec's golden, else
    the cross-check would be meaningless."""
    ref = reference_1040(TaxFacts(filing_status="single", wages=40_000,
                                  fed_withholding=3000))
    assert ref["line_15"] == 24_250
    assert ref["line_16"] == 2_675
    assert ref["refund"] == 325
    assert ref["owed"] == 0

