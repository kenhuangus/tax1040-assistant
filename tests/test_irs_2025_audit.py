"""
EXTERNAL-TRUTH regression guard for the deterministic TY2025 tax engine.

This file validates `app/taxconstants_2025.py` and `app/compute.py` against
HAND-TYPED IRS 2025 (post-OBBBA) ground-truth values — NOT against the engine's
own constants. Every reference number below is a literal copied from the IRS
2025 figures (Rev. Proc. 2024-40 + OBBBA P.L. 119-21), and every expected
spot-check result is computed BY HAND (arithmetic shown in comments) and frozen
as a literal. If a constant in the engine ever drifts from IRS truth, or a
calculation path regresses, the relevant assertion here fails.

Run ONLY this file:
    python -m pytest tests/test_irs_2025_audit.py -q

Self-consistency sweeps cannot catch a wrong constant (they compare the engine
to itself). This file closes exactly that gap.
"""
from __future__ import annotations

import math

import pytest

from app import taxconstants_2025 as K
from app.compute import compute_1040
from app.models import Dependent, TaxFacts


# ===========================================================================
# EXTERNAL IRS 2025 GROUND TRUTH (hand-typed literals — the source of truth
# for this audit). Do NOT import these from the engine.
# ===========================================================================

EXT_STD_DED = {
    "single": 15_750,
    "mfj": 31_500,
    "mfs": 15_750,
    "hoh": 23_625,
    "qss": 31_500,
}

# (upper_edge, rate). math.inf marks the top (37%) bracket.
EXT_BRACKETS = {
    "single": [
        (11_925, 0.10),
        (48_475, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_525, 0.32),
        (626_350, 0.35),
        (math.inf, 0.37),
    ],
    # MFS: same as Single EXCEPT 35% edge -> 375_800, then 37% above.
    "mfs": [
        (11_925, 0.10),
        (48_475, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_525, 0.32),
        (375_800, 0.35),
        (math.inf, 0.37),
    ],
    "mfj": [
        (23_850, 0.10),
        (96_950, 0.12),
        (206_700, 0.22),
        (394_600, 0.24),
        (501_050, 0.32),
        (751_600, 0.35),
        (math.inf, 0.37),
    ],
    # QSS: same as MFJ.
    "qss": [
        (23_850, 0.10),
        (96_950, 0.12),
        (206_700, 0.22),
        (394_600, 0.24),
        (501_050, 0.32),
        (751_600, 0.35),
        (math.inf, 0.37),
    ],
    "hoh": [
        (17_000, 0.10),
        (64_850, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_500, 0.32),   # 250_500 (NOT single's 250_525)
        (626_350, 0.35),
        (math.inf, 0.37),
    ],
}

# Credits.
EXT_CTC = 2_200            # per qualifying child under 17
EXT_ODC = 500             # credit for other dependents (nonrefundable)
EXT_ACTC_CAP = 1_700       # refundable Additional CTC cap per child
EXT_CTC_PHASEOUT_START = {  # MAGI above which CTC/ODC reduces $50 per $1,000
    "single": 200_000,
    "mfs": 200_000,
    "hoh": 200_000,
    "qss": 400_000,
    "mfj": 400_000,
}
EXT_CTC_PHASEOUT_INCREMENT = 1_000
EXT_CTC_PHASEOUT_REDUCTION = 50

# EITC 2025 (secondary — verified if implemented).
EXT_EITC_MAX = {"0": 649, "1": 4_328, "2": 7_152, "3+": 8_046}
EXT_EITC_1CHILD_POS_SINGLE = 23_350   # 1-child phaseout begins (single/hoh)
EXT_EITC_1CHILD_POS_MFJ = 30_470      # 1-child phaseout begins (mfj)
EXT_EITC_1CHILD_PO_RATE = 0.1598      # phaseout rate
EXT_EITC_1CHILD_RATE = 0.34           # credit (phase-in) rate
EXT_EITC_1CHILD_PI_END = 12_730       # earned income at which max is reached


# ===========================================================================
# PART 1 — CONSTANT AUDIT (engine constant vs external IRS truth)
# ===========================================================================

@pytest.mark.parametrize("status", ["single", "mfj", "mfs", "hoh", "qss"])
def test_standard_deduction_matches_irs(status):
    assert K.STD_DED[status] == EXT_STD_DED[status], (
        f"STD_DED[{status}] expected {EXT_STD_DED[status]}, got {K.STD_DED[status]}"
    )


@pytest.mark.parametrize("status", ["single", "mfj", "mfs", "hoh", "qss"])
def test_bracket_edges_match_irs(status):
    assert K.BRACKETS[status] == EXT_BRACKETS[status], (
        f"BRACKETS[{status}] expected {EXT_BRACKETS[status]}, got {K.BRACKETS[status]}"
    )


def test_ctc_constant():
    assert K.CTC == EXT_CTC, f"CTC expected {EXT_CTC}, got {K.CTC}"


def test_odc_constant():
    assert K.ODC == EXT_ODC, f"ODC expected {EXT_ODC}, got {K.ODC}"


def test_actc_cap_constant():
    assert K.ACTC_CAP == EXT_ACTC_CAP, f"ACTC_CAP expected {EXT_ACTC_CAP}, got {K.ACTC_CAP}"


@pytest.mark.parametrize("status", ["single", "mfj", "mfs", "hoh", "qss"])
def test_ctc_phaseout_start(status):
    assert K.CTC_PHASEOUT[status] == EXT_CTC_PHASEOUT_START[status], (
        f"CTC_PHASEOUT[{status}] expected {EXT_CTC_PHASEOUT_START[status]}, "
        f"got {K.CTC_PHASEOUT[status]}"
    )


def test_ctc_phaseout_step():
    assert K.CTC_PHASEOUT_INCREMENT == EXT_CTC_PHASEOUT_INCREMENT
    assert K.CTC_PHASEOUT_REDUCTION_PER_INCREMENT == EXT_CTC_PHASEOUT_REDUCTION


def test_eitc_max_credits():
    assert K.EITC["0children"]["single"]["max_credit"] == EXT_EITC_MAX["0"]
    assert K.EITC["1child"]["single"]["max_credit"] == EXT_EITC_MAX["1"]
    assert K.EITC["2children"]["single"]["max_credit"] == EXT_EITC_MAX["2"]
    assert K.EITC["3plusChildren"]["single"]["max_credit"] == EXT_EITC_MAX["3+"]


def test_eitc_1child_phaseout_params():
    band_s = K.EITC["1child"]["single"]
    band_j = K.EITC["1child"]["mfj"]
    assert band_s["phase_out_start"] == EXT_EITC_1CHILD_POS_SINGLE
    assert band_j["phase_out_start"] == EXT_EITC_1CHILD_POS_MFJ
    assert band_s["phase_out_rate"] == EXT_EITC_1CHILD_PO_RATE
    assert band_s["phase_in_rate"] == EXT_EITC_1CHILD_RATE
    assert band_s["phase_in_end"] == EXT_EITC_1CHILD_PI_END


# ===========================================================================
# PART 2 — EXTERNAL HAND-COMPUTED SPOT-CHECKS
# Each expected number is computed BY HAND from the IRS reference values above
# (arithmetic shown in the comment) and frozen as a literal. Drive
#     wages = taxable + standard_deduction
# so that line_15 (taxable) equals the target, with no dependents unless noted.
# ===========================================================================

def _facts(status, wages, deps=None, withholding=0):
    return TaxFacts(
        filing_status=status,
        wages=wages,
        fed_withholding=withholding,
        dependents=deps or [],
    )


def _expect_line16(status, taxable, wages):
    r = compute_1040(_facts(status, wages))
    assert r.line_15 == taxable, f"setup error: line_15 {r.line_15} != {taxable}"
    return r


# --- a. Single taxable 24,250  (GOLDEN) -------------------------------------
# wages = 24,250 + 15,750 = 40,000.
# taxable < 100k -> midpoint of $50 row containing 24,250:
#   base = (24250//50)*50 + 25 = 24250 + 25 = 24275.
# bracket: 11925*.10 = 1192.50
#        + (24275-11925)*.12 = 12350*.12 = 1482.00
#        = 2674.50 -> round half up = 2675.
def test_a_single_24250_golden():
    r = _expect_line16("single", 24_250, 40_000)
    assert r.line_16 == 2_675


# --- b. Single taxable 50,000 ----------------------------------------------
# wages = 50,000 + 15,750 = 65,750.
# base = (50000//50)*50+25 = 50025.
# 11925*.10=1192.50; (48475-11925)*.12=36550*.12=4386.00;
# (50025-48475)*.22=1550*.22=341.00; sum=5919.50 -> 5920.
def test_b_single_50000():
    r = _expect_line16("single", 50_000, 65_750)
    assert r.line_16 == 5_920


# --- c. Single taxable 100,000  (REGIME BOUNDARY: exact, no midpoint) -------
# wages = 100,000 + 15,750 = 115,750.  taxable == 100k -> EXACT figure.
# 11925*.10=1192.50; (48475-11925)*.12=4386.00; (100000-48475)*.22=51525*.22=11335.50;
# sum=16914.00 -> 16914.
def test_c_single_100000_boundary():
    r = _expect_line16("single", 100_000, 115_750)
    assert r.line_16 == 16_914


# --- d. Single taxable 150,000 ---------------------------------------------
# wages = 150,000 + 15,750 = 165,750.  taxable>=100k -> exact.
# 1192.50 + 4386.00 + (103350-48475)*.22=54875*.22=12072.50
#         + (150000-103350)*.24=46650*.24=11196.00 = 28847.00 -> 28847.
def test_d_single_150000():
    r = _expect_line16("single", 150_000, 165_750)
    assert r.line_16 == 28_847


# --- e. MFJ taxable 50,000 -------------------------------------------------
# wages = 50,000 + 31,500 = 81,500.  base = 50025.
# 23850*.10=2385.00; (50025-23850)*.12=26175*.12=3141.00; sum=5526.00 -> 5526.
def test_e_mfj_50000():
    r = _expect_line16("mfj", 50_000, 81_500)
    assert r.line_16 == 5_526


# --- f. MFJ taxable 250,000 ------------------------------------------------
# wages = 250,000 + 31,500 = 281,500.  taxable>=100k -> exact.
# 23850*.10=2385.00; (96950-23850)*.12=73100*.12=8772.00;
# (206700-96950)*.22=109750*.22=24145.00; (250000-206700)*.24=43300*.24=10392.00;
# sum=45694.00 -> 45694.
def test_f_mfj_250000():
    r = _expect_line16("mfj", 250_000, 281_500)
    assert r.line_16 == 45_694


# --- g. MFS taxable 400,000  (above the MFS-specific 375,800 37% edge) ------
# wages = 400,000 + 15,750 = 415,750.  taxable>=100k -> exact.
# 11925*.10=1192.50; (48475-11925)*.12=4386.00; (103350-48475)*.22=12072.50;
# (197300-103350)*.24=93950*.24=22548.00; (250525-197300)*.32=53225*.32=17032.00;
# (375800-250525)*.35=125275*.35=43846.25; (400000-375800)*.37=24200*.37=8954.00;
# sum=110031.25 -> round half up = 110031.
def test_g_mfs_400000_top_bracket():
    r = _expect_line16("mfs", 400_000, 415_750)
    assert r.line_16 == 110_031


# --- h. HoH taxable 60,000 -------------------------------------------------
# wages = 60,000 + 23,625 = 83,625.  base = 60025.
# 17000*.10=1700.00; (60025-17000)*.12... wait edge 64850 not yet reached:
# (60025-17000)=43025 within 12% band -> 43025*.12=5163.00; sum=6863.00 -> 6863.
def test_h_hoh_60000():
    r = _expect_line16("hoh", 60_000, 83_625)
    assert r.line_16 == 6_863


# --- i. HoH taxable 260,000  (above the HoH-specific 250,500 32% edge) ------
# wages = 260,000 + 23,625 = 283,625.  taxable>=100k -> exact.
# 17000*.10=1700.00; (64850-17000)*.12=47850*.12=5742.00;
# (103350-64850)*.22=38500*.22=8470.00; (197300-103350)*.24=93950*.24=22548.00;
# (250500-197300)*.32=53200*.32=17024.00; (260000-250500)*.35=9500*.35=3325.00;
# sum=58809.00 -> 58809.
def test_i_hoh_260000_top_band():
    r = _expect_line16("hoh", 260_000, 283_625)
    assert r.line_16 == 58_809


# --- j. QSS taxable 120,000  (same brackets as MFJ) ------------------------
# wages = 120,000 + 31,500 = 151,500.  taxable>=100k -> exact.
# 23850*.10=2385.00; (96950-23850)*.12=8772.00; (120000-96950)*.22=23050*.22=5071.00;
# sum=16228.00 -> 16228.
def test_j_qss_120000():
    r = _expect_line16("qss", 120_000, 151_500)
    assert r.line_16 == 16_228


# --- k. Single 40,000 wages + 1 qualifying child under 17 -------------------
# wages 40,000 -> taxable 24,250 -> line16 2,675 (case a).
# CTC 2,200 (AGI 40k < 200k phaseout) -> line_19 = 2,200; tax after = 475.
# EITC 1 child single: earned 40,000 >= phase_in_end 12,730 -> credit 4,328;
#   phaseout: (40,000 - 23,350)*.1598 = 16,650*.1598 = 2,660.67;
#   4,328 - 2,660.67 = 1,667.33 -> round half up = 1,667.
# payments = 3,120 withholding + 1,667 EITC = 4,787; refund = 4,787 - 475 = 4,312.
def test_k_single_40000_one_child():
    deps = [Dependent(name="Kid", ssn="111-22-3333", is_under_17=True, has_ssn=True)]
    r = compute_1040(_facts("single", 40_000, deps, withholding=3_120))
    assert r.line_15 == 24_250
    assert r.line_16 == 2_675
    assert r.line_19 == 2_200          # CTC
    assert r.line_22 == 475            # tax after nonrefundable credits
    assert r.line_27 == 1_667          # EITC
    assert r.line_25d == 3_120         # withholding
    assert r.line_33 == 4_787          # total payments (3120 + 1667 EITC + 0 ACTC)
    assert r.line_28 == 0              # no leftover CTC -> no ACTC
    assert r.refund == 4_312
    assert r.owed == 0


# --- l. Single 40,000 wages + 1 ADULT dependent -> ODC 500 ------------------
# Adult dependent (not under 17) -> Other Dependent. line_19 = ODC = 500
# (tax 2,675 > 500, so full 500 allowed; AGI 40k < phaseout).
def test_l_single_40000_one_adult_dependent():
    deps = [Dependent(name="Parent", ssn="444-55-6666", is_under_17=False, has_ssn=True)]
    r = compute_1040(_facts("single", 40_000, deps))
    assert r.line_19 == 500
    assert r.line_28 == 0              # ODC is not refundable
    assert r.line_27 == 0              # no EITC qualifying children; 40k single, 0-kid POS 10,620 -> 0


# --- m. Low income: single 20,000 wages + 2 children -> refundable ACTC -----
# wages 20,000 -> taxable = 20,000 - 15,750 = 4,250 -> line16:
#   base = (4250//50)*50+25 = 4275; 4275*.10 = 427.50 -> 428.
# Nonrefundable CTC capped at tax: line_19 = min(2*2200=4400, 428) = 428.
# Additional CTC (line 28): leftover CTC = 4400 - 428 = 3972;
#   earned_based = 15% * (20,000 - 2,500) = .15*17,500 = 2,625;
#   per_child_cap = 1,700 * 2 = 3,400;
#   line_28 = min(2,625, 3,400, 3,972) = 2,625  (capped by the 15% formula here;
#   the $1,700/child cap is present and binding-checked but not the active limit).
def test_m_low_income_two_children_actc():
    deps = [
        Dependent(name="A", ssn="111-11-1111", is_under_17=True, has_ssn=True),
        Dependent(name="B", ssn="222-22-2222", is_under_17=True, has_ssn=True),
    ]
    r = compute_1040(_facts("single", 20_000, deps))
    assert r.line_15 == 4_250
    assert r.line_16 == 428
    assert r.line_19 == 428            # nonrefundable CTC capped at tax
    assert r.line_28 == 2_625          # refundable ACTC appears
    assert r.line_28 > 0
    # ACTC must never exceed the $1,700/child cap.
    assert r.line_28 <= EXT_ACTC_CAP * 2


# --- m2. ACTC cap is genuinely binding when earned income is very high ------
# Make 15%*(wages-2500) and leftover both exceed the per-child cap, forcing the
# $1,700/child cap to be the active limit. Use 1 child WITHOUT SSN so it is NOT
# a qualifying child... no — we need a qualifying child with ~0 tax. Instead use
# a contrived high-wage but the tax would consume the CTC. The cleanest binding
# test: a low tax with high enough earned income that earned_based > cap.
# Single 1 child, wages 16,000 -> taxable 250 -> tax 25 (midpoint 25*.10=2.5->3?).
# Better: pick wages so earned_based > 1700 and tax tiny. wages=15,800 ->
#   taxable=50 -> base=(50//50)*50+25=75 -> 75*.10=7.5 -> 8 tax.
#   CTC full 2200, line_19=min(2200,8)=8; leftover=2192.
#   earned_based=.15*(15800-2500)=.15*13300=1995; per_child_cap=1700.
#   line_28=min(1995,1700,2192)=1700  <-- the $1,700/child cap is the ACTIVE limit.
def test_m2_actc_per_child_cap_binding():
    deps = [Dependent(name="C", ssn="333-33-3333", is_under_17=True, has_ssn=True)]
    r = compute_1040(_facts("single", 15_800, deps))
    assert r.line_15 == 50
    assert r.line_28 == 1_700          # exactly the $1,700/child cap
    assert r.line_28 == EXT_ACTC_CAP


# --- n. High income inside CTC phaseout band: single, MAGI 215,000, 1 child -
# Drive wages = MAGI = 215,000 (AGI == wages). taxable = 215,000 - 15,750 = 199,250.
#   line16 (exact): 1192.50 + 4386.00 + (103350-48475)*.22=12072.50
#     + (197300-103350)*.24=93950*.24=22548.00 + (199250-197300)*.32=1950*.32=624.00
#     = 40823.00 -> 40823.
# CTC phaseout: excess = 215,000 - 200,000 = 15,000; increments = ceil(15000/1000)=15;
#   reduction = 15 * 50 = 750; CTC after = 2,200 - 750 = 1,450.
#   line_19 = min(1,450, tax 40,823) = 1,450.
def test_n_ctc_phaseout_band():
    deps = [Dependent(name="Kid", ssn="555-55-5555", is_under_17=True, has_ssn=True)]
    r = compute_1040(_facts("single", 215_000, deps))
    assert r.line_15 == 199_250
    assert r.line_16 == 40_823
    assert r.line_19 == 1_450          # CTC reduced by $750 ($50 * 15 increments)
    assert r.line_28 == 0              # high earner: leftover CTC consumed by tax


# --- n2. Phaseout fraction rounds UP ($50 per $1,000 OR FRACTION) -----------
# Single, 1 child, MAGI 200,001 -> excess 1 -> ceil(1/1000)=1 increment ->
#   reduction $50 -> CTC after = 2,150.  Confirms fractional excess rounds up.
def test_n2_ctc_phaseout_rounds_up_fraction():
    deps = [Dependent(name="Kid", ssn="666-66-6666", is_under_17=True, has_ssn=True)]
    r = compute_1040(_facts("single", 200_001, deps))
    assert r.line_19 == 2_150          # 2,200 - 50 (one increment for $1 over)
