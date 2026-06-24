"""
Frozen TY2025 federal tax constants — copied VERBATIM (diff-verified) from
`C:\\Users\\kenhu\\novatax\\services\\taxConstants\\2025.ts`.

Authoritative sources (cited in NovaTax 2025.ts):
  - Federal income-tax brackets & standard deductions: IRS Rev. Proc. 2024-40
    (standard deductions reflect OBBBA P.L. 119-21, 2025, which raised the
    §63(c) amounts above the Rev. Proc. base for TY2025).
  - Child Tax Credit $2,200 / phaseouts: OBBBA (H.R.1, 2025) §24; IRS 2025 guidance.
  - Credit for Other Dependents $500: §24(h)(4).
  - Additional Child Tax Credit (refundable): IRC §24(h)(5), Schedule 8812 (2025) —
    cap $1,700/child, earned-income floor $2,500, 15% rate.
  - EITC table & investment-income limit $11,950: IRS Rev. Proc. 2024-40 §3.06
    (§32 / Pub 596 phase-in/phase-out). https://www.irs.gov/pub/irs-drop/rp-24-40.pdf

Money values are whole-dollar ints. Brackets are ordered lists of
(upper_limit, rate) tuples; the final tuple uses `math.inf` for the top bracket.
Keys use the short filing-status codes from app.models.FilingStatus:
  "single", "mfj", "mfs", "hoh", "qss".
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Standard deductions — IRS Rev. Proc. 2024-40 (OBBBA TY2025 amounts).
# ---------------------------------------------------------------------------
STD_DED: dict[str, int] = {
    "single": 15750,
    "mfj": 31500,
    "mfs": 15750,
    "hoh": 23625,
    "qss": 31500,
}

# ---------------------------------------------------------------------------
# Federal income-tax brackets — IRS Rev. Proc. 2024-40.
# Each entry: (upper_limit, marginal_rate). Ordered ascending; top = math.inf.
#
# NOTE the deliberate quirk verbatim from NovaTax 2025.ts:
#   - MFS 35% bracket top is 375800 (= half of MFJ 751600), NOT 626350.
#   - HoH 32% bracket top is 250500, NOT 250525 (single's value).
# These are copied exactly as the authoritative source has them.
# ---------------------------------------------------------------------------
BRACKETS: dict[str, list[tuple[float, float]]] = {
    "single": [
        (11925, 0.10),
        (48475, 0.12),
        (103350, 0.22),
        (197300, 0.24),
        (250525, 0.32),
        (626350, 0.35),
        (math.inf, 0.37),
    ],
    "mfj": [
        (23850, 0.10),
        (96950, 0.12),
        (206700, 0.22),
        (394600, 0.24),
        (501050, 0.32),
        (751600, 0.35),
        (math.inf, 0.37),
    ],
    # MFS: exactly half of MFJ bracket limits (IRS Rev. Proc. 2024-40).
    "mfs": [
        (11925, 0.10),
        (48475, 0.12),
        (103350, 0.22),
        (197300, 0.24),
        (250525, 0.32),
        (375800, 0.35),
        (math.inf, 0.37),
    ],
    "hoh": [
        (17000, 0.10),
        (64850, 0.12),
        (103350, 0.22),
        (197300, 0.24),
        (250500, 0.32),   # 250500 (NOT 250525) — verbatim from source
        (626350, 0.35),
        (math.inf, 0.37),
    ],
    # QSS: same as MFJ.
    "qss": [
        (23850, 0.10),
        (96950, 0.12),
        (206700, 0.22),
        (394600, 0.24),
        (501050, 0.32),
        (751600, 0.35),
        (math.inf, 0.37),
    ],
}

# ---------------------------------------------------------------------------
# Child Tax Credit / Credit for Other Dependents.
# ---------------------------------------------------------------------------
CTC: int = 2200            # §24 CTC per qualifying child (OBBBA TY2025)
ODC: int = 500             # §24(h)(4) Credit for Other Dependents

# CTC/ODC phaseout thresholds (MAGI above which the credit reduces $50 per
# $1,000). NovaTax 2025.ts defines single=200000, mfj=400000; the remaining
# statuses follow §24(b)(2): MFS=200000, HoH=200000, QSS (joint) = 400000.
CTC_PHASEOUT: dict[str, int] = {
    "single": 200000,
    "mfj": 400000,
    "mfs": 200000,
    "hoh": 200000,
    "qss": 400000,
}
CTC_PHASEOUT_INCREMENT: int = 1000           # ctcPhaseoutIncrement
CTC_PHASEOUT_REDUCTION_PER_INCREMENT: int = 50  # ctcPhaseoutReductionPerIncrement

# ---------------------------------------------------------------------------
# Additional Child Tax Credit (refundable) — Schedule 8812 (2025).
# ---------------------------------------------------------------------------
ACTC_CAP: int = 1700       # actcRefundableCapPerChild
ACTC_FLOOR: int = 2500     # actcEarnedIncomeFloor
ACTC_RATE: float = 0.15    # actcRefundableRate

# ---------------------------------------------------------------------------
# Earned Income Tax Credit — IRS Rev. Proc. 2024-40 §3.06 (Pub 596 formula).
# Structure copied verbatim from NovaTax 2025.ts `eitc`:
#   EITC[<children-bucket>][<status>] = {
#       phase_in_rate, phase_in_end, max_credit,
#       phase_out_start, phase_out_rate, phase_out_end }
# Buckets: "0children", "1child", "2children", "3plusChildren".
# Status columns: "single" (used by single/hoh/qss-non-joint) and "mfj"
# (used by mfj and qss-as-joint).
# ---------------------------------------------------------------------------
EITC: dict[str, dict[str, dict[str, float]]] = {
    "0children": {
        "single": {"phase_in_rate": 0.0765, "phase_in_end": 8490,  "max_credit": 649,  "phase_out_start": 10620, "phase_out_rate": 0.0765, "phase_out_end": 19104},
        "mfj":    {"phase_in_rate": 0.0765, "phase_in_end": 8490,  "max_credit": 649,  "phase_out_start": 17730, "phase_out_rate": 0.0765, "phase_out_end": 26214},
    },
    "1child": {
        "single": {"phase_in_rate": 0.3400, "phase_in_end": 12730, "max_credit": 4328, "phase_out_start": 23350, "phase_out_rate": 0.1598, "phase_out_end": 50434},
        "mfj":    {"phase_in_rate": 0.3400, "phase_in_end": 12730, "max_credit": 4328, "phase_out_start": 30470, "phase_out_rate": 0.1598, "phase_out_end": 57554},
    },
    "2children": {
        "single": {"phase_in_rate": 0.4000, "phase_in_end": 17880, "max_credit": 7152, "phase_out_start": 23350, "phase_out_rate": 0.2106, "phase_out_end": 57310},
        "mfj":    {"phase_in_rate": 0.4000, "phase_in_end": 17880, "max_credit": 7152, "phase_out_start": 30470, "phase_out_rate": 0.2106, "phase_out_end": 64430},
    },
    "3plusChildren": {
        "single": {"phase_in_rate": 0.4500, "phase_in_end": 17880, "max_credit": 8046, "phase_out_start": 23350, "phase_out_rate": 0.2106, "phase_out_end": 61555},
        "mfj":    {"phase_in_rate": 0.4500, "phase_in_end": 17880, "max_credit": 8046, "phase_out_start": 30470, "phase_out_rate": 0.2106, "phase_out_end": 68675},
    },
}
EITC_INVESTMENT_LIMIT: int = 11950   # eitcInvestmentIncomeLimit — §32(i)
