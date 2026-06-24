"""
Deterministic Form 1040 tax core (NO LLM) for TY2025.

Implements EXACTLY the compute sequence in `docs/architecture.md` §4. Every
money line is a whole-dollar int, rounded half-up. The Line-16 tax uses the IRS
Tax Table midpoint rule for taxable income < $100,000:

    base = (taxable // 50) * 50 + 25      # midpoint of the $50 table row
    tax  = round_half_up(bracket_tax(base, status))

For taxable >= $100,000 the Tax Computation Worksheet uses the exact figure
(no midpoint). `bracket_tax` walks the marginal brackets exactly like NovaTax
`taxEngine.ts::calculateProgressiveTax`.

Public API:
    compute_1040(facts: TaxFacts) -> Form1040Result
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

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
    EITC_INVESTMENT_LIMIT,
    ODC,
    STD_DED,
)

_TAX_TABLE_CUTOFF = 100_000


def round_half_up(amount: float | Decimal) -> int:
    """Round to the nearest whole dollar, halves rounding up (away from zero
    for positives). Matches IRS whole-dollar rounding."""
    return int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def bracket_tax(income: float, status: str) -> float:
    """Walk the marginal brackets for `status` (ordered (upper_limit, rate)
    tuples). Mirrors taxEngine.ts::calculateProgressiveTax exactly."""
    brackets = BRACKETS[status]
    tax = 0.0
    previous_limit = 0.0
    for upper_limit, rate in brackets:
        if income > previous_limit:
            taxable_in_band = min(income, upper_limit) - previous_limit
            tax += taxable_in_band * rate
            previous_limit = upper_limit
        else:
            break
    return tax


def tax(taxable: int, status: str) -> int:
    """Form 1040 Line 16. IRS Tax Table midpoint rule under $100k; exact
    Tax Computation Worksheet at/above $100k. Whole dollars, half-up."""
    if taxable <= 0:
        return 0
    if taxable >= _TAX_TABLE_CUTOFF:
        base: float = float(taxable)
    else:
        # Midpoint of the $50-wide IRS Tax Table row containing `taxable`.
        base = (taxable // 50) * 50 + 25
    return round_half_up(bracket_tax(base, status))


def _eitc_column(status: str) -> str:
    """EITC uses a 'single' column for single/hoh/qss-non-joint and an 'mfj'
    column for mfj. Mirrors taxEngine.ts (`isJoint ? 'mfj' : 'single'`)."""
    return "mfj" if status in ("mfj", "qss") else "single"


def _eitc(wages: int, agi: int, status: str, qualifying_children: int) -> int:
    """Earned Income Credit (Form 1040 line 27), refundable. Mirrors the
    NovaTax engine's EITC block. For this wages-only profile earned income ==
    AGI == wages and investment/disqualified income == 0.

    MFS is ineligible here (the engine only allows MFS-with-children that lived
    apart all year, which is out of scope) → returns 0 for status == 'mfs'.
    """
    if wages <= 0:
        return 0
    if status == "mfs":
        return 0
    # Disqualified investment income is 0 in a wages-only return; the check is
    # therefore always satisfied, but kept explicit for parity with the engine.
    if 0 > EITC_INVESTMENT_LIMIT:  # pragma: no cover - structural placeholder
        return 0

    bucket = {0: "0children", 1: "1child", 2: "2children"}.get(
        min(qualifying_children, 3), "3plusChildren"
    )
    band = EITC[bucket][_eitc_column(status)]

    earned = float(wages)
    # Phase-in: credit reaches max_credit at phase_in_end.
    if earned >= band["phase_in_end"]:
        credit = band["max_credit"]
    else:
        credit = earned * band["phase_in_rate"]

    # Phase-out: based on higher of earned income or AGI (IRS Pub 596).
    phase_base = max(earned, float(agi))
    if phase_base > band["phase_out_start"]:
        reduction = (phase_base - band["phase_out_start"]) * band["phase_out_rate"]
        credit = max(0.0, credit - reduction)

    return round_half_up(credit)


def _split_dependents(dependents: list[Dependent]) -> tuple[int, int]:
    """Return (qualifying_children, other_dependents).

    A qualifying child for the CTC is under 17 AND has an SSN. Everyone else
    (including under-17 without an SSN, and all 17+) is an 'other dependent'
    eligible for the $500 ODC.
    """
    qc = sum(1 for d in dependents if d.is_under_17 and d.has_ssn)
    od = len(dependents) - qc
    return qc, od


def _ctc_phaseout_reduction(agi: int, status: str) -> int:
    """CTC/ODC phaseout: $50 reduction per $1,000 (or fraction) of AGI over the
    status threshold. Returns the total dollar reduction to the combined credit.
    Not triggered below the threshold (e.g. a $40k filer)."""
    threshold = CTC_PHASEOUT[status]
    if agi <= threshold:
        return 0
    excess = agi - threshold
    # Round the excess UP to the next whole increment, per §24(b)(2).
    increments = -(-excess // CTC_PHASEOUT_INCREMENT)  # ceil division
    return increments * CTC_PHASEOUT_REDUCTION_PER_INCREMENT


def compute_1040(facts: TaxFacts) -> Form1040Result:
    """Compute every populated Form 1040 line for the supported (W-2-only)
    scope. Deterministic; see docs/architecture.md §4."""
    status = facts.filing_status
    wages = int(facts.wages)
    withholding = int(facts.fed_withholding)
    dependents = list(facts.dependents)

    r = Form1040Result()

    # 1. Income / AGI (no adjustments in scope).
    r.line_1a = wages
    r.line_1z = wages
    r.line_9 = wages
    r.line_11 = wages  # AGI

    # 2-3. Deduction and taxable income.
    r.line_12 = STD_DED[status]
    r.line_13 = 0
    r.line_14 = r.line_12 + r.line_13
    r.line_15 = max(0, r.line_11 - r.line_14)  # taxable income

    # 4. Tax before credits.
    r.line_16 = tax(r.line_15, status)
    r.line_17 = 0
    r.line_18 = r.line_16 + r.line_17

    # 5. Credits.
    qc, od = _split_dependents(dependents)
    agi = r.line_11

    # Nonrefundable CTC + ODC (Form 1040 line 19), capped at tax (line 18).
    ctc_odc_full = qc * CTC + od * ODC
    if ctc_odc_full > 0:
        reduction = _ctc_phaseout_reduction(agi, status)
        ctc_odc_after_phaseout = max(0, ctc_odc_full - reduction)
    else:
        ctc_odc_after_phaseout = 0
    r.line_19 = min(ctc_odc_after_phaseout, r.line_18)

    # Additional Child Tax Credit — refundable (Form 1040 line 28).
    # Refundable amount = min(15% * max(0, earned - 2500), 1700 * qc, leftover
    # CTC that couldn't be used nonrefundably). ODC is not refundable.
    if qc > 0:
        ctc_only_full = min(qc * CTC, max(0, ctc_odc_after_phaseout - od * ODC))
        # CTC actually used nonrefundably = portion of line_19 attributable to
        # the CTC (ODC is applied within the same cap; the nonrefundable CTC is
        # whatever of line_19 remains after ODC, floored at 0).
        ctc_used_nonrefundable = max(0, r.line_19 - od * ODC)
        leftover_ctc = max(0, ctc_only_full - ctc_used_nonrefundable)
        earned_based = round_half_up(ACTC_RATE * max(0, wages - ACTC_FLOOR))
        per_child_cap = ACTC_CAP * qc
        r.line_28 = max(0, min(earned_based, per_child_cap, leftover_ctc))
    else:
        r.line_28 = 0

    # 6. Total tax.
    r.line_20 = 0
    r.line_21 = r.line_19 + r.line_20
    r.line_22 = max(0, r.line_18 - r.line_21)
    r.line_23 = 0
    r.line_24 = r.line_22 + r.line_23

    # 7. EITC — refundable (Form 1040 line 27).
    r.line_27 = _eitc(wages, agi, status, qc)

    # 8. Payments.
    r.line_25a = withholding
    r.line_25d = withholding
    r.line_32 = r.line_27 + r.line_28
    r.line_33 = r.line_25d + r.line_32

    # 9. Refund vs amount owed (exactly one nonzero).
    if r.line_33 > r.line_24:
        r.line_34 = r.line_33 - r.line_24
        r.line_35a = r.line_34
        r.line_37 = 0
    else:
        r.line_37 = r.line_24 - r.line_33
        r.line_34 = 0
        r.line_35a = 0

    # Convenience mirrors (mutually exclusive).
    r.refund = r.line_34
    r.owed = r.line_37

    return r
