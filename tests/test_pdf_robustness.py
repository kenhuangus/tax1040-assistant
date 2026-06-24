"""
Robustness suite for ``app.pdf_fill.fill_1040_pdf`` — proving "download ALWAYS
works".

The single promise under test: for ANY computed result the assistant can produce
— any dollar amount (tiny $1 wages through huge 7-8 figure numbers), any of the
five filing statuses, refund / owe / exact-zero outcomes, and 0-4 dependents
(CTC child or ODC adult) — ``fill_1040_pdf`` must, with both ``flatten=True`` and
``flatten=False``:

  1. never raise (the core promise — fill never crashes),
  2. return a path to a file that exists, begins ``b"%PDF-"``, is > 50 KB, and
     has exactly 2 pages,
  3. (flatten=True) have its AcroForm removed and the key dollar amounts baked
     into the rendered page text WITH thousands commas,
  4. (flatten=False) carry the expected comma-formatted strings in the AcroForm
     ``/V`` values and set the correct filing-status checkbox on-value (/1../5),
  5. render large numbers with thousands commas — and we *report* (not fail)
     whether any dollar field looks truncated for 8-figure values.

This file is self-contained: it owns ONLY itself, writes every artifact to a
private ``tempfile.mkdtemp`` directory (never into ``out/`` or the repo), and
reads the live ``field_spec.json`` so field ids stay canonical.

Run ONLY this file (another agent edits the rest of the suite):

    <python> -m pytest tests/test_pdf_robustness.py -q
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from pypdf import PdfReader

# Make the repo root importable when pytest is invoked from anywhere.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.models import Dependent, Form1040Result, Identity  # noqa: E402
from app.pdf_fill import fill_1040_pdf  # noqa: E402

# --------------------------------------------------------------------------- #
# Field ids — read from the SAME source of truth pdf_fill uses, so this suite
# can never drift from the verified map.
# --------------------------------------------------------------------------- #
_SPEC = json.loads(
    (_REPO / "assets" / "irs" / "_probe" / "field_spec.json").read_text("utf-8")
)
_LINES = _SPEC["lines_dollar"]
_STATUS = _SPEC["filing_status_ONEOF"]

F_FIRST = _SPEC["identity"]["your_first_mi"]
F_LAST = _SPEC["identity"]["your_last"]
F_SSN = _SPEC["identity"]["your_ssn"]

# The four "key amounts" the task calls out, each tagged with the page it lands
# on (1-indexed) for the flattened extract_text check. line_1a/line_11 share a
# value so it shows on both pages; that's fine — we look on its own page.
F_1A = _LINES["1a_wages"]            # page 1
F_16 = _LINES["16_tax"]             # page 2
F_25A = _LINES["25a_W2_withholding"]  # page 2
F_34 = _LINES["34_overpaid_refund"]   # page 2 (refund)
F_37 = _LINES["37_amount_owed"]      # page 2 (owe)

# One module-scoped temp dir for ALL artifacts (NOT out/, NOT the repo).
_OUTDIR = Path(tempfile.mkdtemp(prefix="pdf_robustness_"))

# A module-level ledger so the final summary test can report truncations / the
# largest fully-rendered amount across every generated case.
_LEDGER: dict[str, list] = {
    "cases": [],          # (case_id, flatten, size, ok)
    "truncated": [],      # (case_id, field_label, expected, rendered_context)
    "max_full_amount": 0,  # largest int that rendered with full commas intact
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fmt(n: int) -> str:
    """Mirror pdf_fill._fmt_money for whole-dollar ints (commas; '0' for zero)."""
    n = int(n)
    return ("-" + f"{-n:,}") if n < 0 else f"{n:,}"


def _field_value(reader: PdfReader, name: str):
    fields = reader.get_fields() or {}
    f = fields.get(name)
    return None if f is None else f.get("/V")


def _assert_valid_pdf(path: str, case_id: str, flatten: bool) -> PdfReader:
    """Shared structural promise: exists, %PDF- header, >50KB, 2 pages."""
    p = Path(path)
    assert p.exists(), f"[{case_id}] returned path does not exist: {path}"
    data = p.read_bytes()
    assert data[:5] == b"%PDF-", f"[{case_id}] not a PDF (header={data[:5]!r})"
    size = len(data)
    assert size > 50_000, f"[{case_id}] PDF suspiciously small ({size} bytes)"
    reader = PdfReader(path)
    assert len(reader.pages) == 2, (
        f"[{case_id}] expected 2 pages, got {len(reader.pages)}"
    )
    _LEDGER["cases"].append((case_id, flatten, size, True))
    return reader


def _page_text(reader: PdfReader, page_index: int) -> str:
    return reader.pages[page_index].extract_text() or ""


def _check_amount_rendered(
    reader: PdfReader,
    case_id: str,
    label: str,
    amount: int,
    page_index: int,
) -> bool:
    """Flattened-PDF check: the comma-formatted amount appears in the page text.

    Records a truncation into the ledger (rather than only asserting) so the
    final summary can report precisely which 8-figure values clipped. Returns
    True when the full formatted string is present.
    """
    if amount == 0:
        # zero is allowed to collide with other "0"s on the form; the dedicated
        # zero-case test covers it. Don't gate big-number rendering on it.
        return True
    text = _page_text(reader, page_index)
    formatted = _fmt(amount)
    present = formatted in text
    if not present:
        # Capture a small window around the bare digits (no commas) if we can
        # find them, so the report shows HOW it rendered (e.g. truncated tail).
        bare = str(amount)
        ctx = ""
        idx = text.find(bare[:4]) if len(bare) >= 4 else -1
        if idx != -1:
            ctx = text[max(0, idx - 4): idx + len(bare) + 6].replace("\n", " ")
        _LEDGER["truncated"].append((case_id, label, formatted, ctx))
    else:
        _LEDGER["max_full_amount"] = max(_LEDGER["max_full_amount"], int(amount))
    return present


# --------------------------------------------------------------------------- #
# Result-variant fixtures (each is internally consistent re: refund vs owe).
# --------------------------------------------------------------------------- #
def _result_refund() -> Form1040Result:
    """Golden single $40k / $3,000 withheld -> $325 refund."""
    return Form1040Result(
        line_1a=40000, line_1z=40000, line_9=40000, line_11=40000,
        line_12=15750, line_14=15750, line_15=24250, line_16=2675,
        line_18=2675, line_22=2675, line_24=2675, line_25a=3000,
        line_25d=3000, line_33=3000, line_34=325, line_35a=325, refund=325,
    )


def _result_owe() -> Form1040Result:
    """$200k wages, under-withheld -> owes $30,000 (line_37 > 0)."""
    return Form1040Result(
        line_1a=200000, line_1z=200000, line_9=200000, line_11=200000,
        line_12=15750, line_14=15750, line_15=184250, line_16=40000,
        line_18=40000, line_22=40000, line_24=40000, line_25a=10000,
        line_25d=10000, line_33=10000, line_37=30000, owed=30000,
    )


def _result_zero() -> Form1040Result:
    """Exact-zero everywhere (no income, no tax, no refund, no owe)."""
    return Form1040Result()


def _result_tiny() -> Form1040Result:
    """$1 of wages — the smallest non-zero case."""
    return Form1040Result(
        line_1a=1, line_1z=1, line_9=1, line_11=1,
        line_12=15750, line_15=0, line_16=0, line_24=0,
        line_25a=1, line_25d=1, line_33=1, line_34=1, line_35a=1, refund=1,
    )


def _result_huge() -> Form1040Result:
    """7-8 figure amounts to stress comma formatting + field width.

    line_1a/line_11 = 9,999,999 (7 digits); line_16 = 3,000,000; line_25a =
    4,000,000; refund = 1,000,000. Also push a couple of lines to 8 digits
    (10,000,000 / 12,345,678) so the truncation report has true 8-figure data.
    """
    return Form1040Result(
        line_1a=9_999_999, line_1z=9_999_999, line_9=12_345_678,
        line_11=9_999_999, line_12=15750, line_15=9_984_249,
        line_16=3_000_000, line_18=3_000_000, line_22=3_000_000,
        line_24=3_000_000, line_25a=4_000_000, line_25d=10_000_000,
        line_33=10_000_000, line_34=1_000_000, line_35a=1_000_000,
        refund=1_000_000,
    )


# variant_key -> (factory, is_owe). is_owe routes the refund/owe page-2 check.
_RESULT_VARIANTS: dict[str, tuple] = {
    "refund": (_result_refund, False),
    "owe": (_result_owe, True),
    "zero": (_result_zero, None),
    "tiny": (_result_tiny, False),
    "huge": (_result_huge, False),
}

_FILING_STATUSES = ["single", "mfj", "mfs", "hoh", "qss"]


def _base_identity() -> Identity:
    return Identity(
        first_name="Jordan A", last_name="Rivera", ssn="123-45-6789",
        address="742 Evergreen Terrace", city="Springfield", state="IL",
        zip="62704",
    )


# --------------------------------------------------------------------------- #
# 1. The big matrix: every filing status x every result variant x flatten.
#    Proves: no exception + structural validity + status checkbox + key amounts.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flatten", [True, False], ids=["flat", "noflat"])
@pytest.mark.parametrize("variant", list(_RESULT_VARIANTS), ids=list(_RESULT_VARIANTS))
@pytest.mark.parametrize("status", _FILING_STATUSES, ids=_FILING_STATUSES)
def test_matrix_status_x_variant_x_flatten(status, variant, flatten):
    factory, is_owe = _RESULT_VARIANTS[variant]
    result = factory()
    ident = _base_identity()
    case_id = f"{status}-{variant}-{'flat' if flatten else 'noflat'}"
    out = str(_OUTDIR / f"{case_id}.pdf")

    # (1) NO EXCEPTION — the core promise.
    ret = fill_1040_pdf(result, ident, status, [], out, flatten=flatten)
    assert ret == out, f"[{case_id}] return value is not the out_path"

    # (2) Structural validity.
    reader = _assert_valid_pdf(out, case_id, flatten)

    if flatten:
        # (3a) AcroForm removed, values baked into rendered text.
        assert "/AcroForm" not in reader.trailer["/Root"], (
            f"[{case_id}] AcroForm survived flatten"
        )
        # Key amounts present with commas, on their own pages.
        _check_amount_rendered(reader, case_id, "line_1a", result.line_1a, 0)
        _check_amount_rendered(reader, case_id, "line_16", result.line_16, 1)
        _check_amount_rendered(reader, case_id, "line_25a", result.line_25a, 1)
        if is_owe:
            _check_amount_rendered(reader, case_id, "line_37", result.line_37, 1)
        elif is_owe is False:
            _check_amount_rendered(reader, case_id, "line_34", result.line_34, 1)
        # zero variant: amounts are all 0 (skipped above) — page-text presence
        # of values is covered by the dedicated zero test.
    else:
        # (3b) AcroForm /V values carry the comma-formatted strings.
        assert _field_value(reader, F_1A) == _fmt(result.line_1a), (
            f"[{case_id}] line_1a /V mismatch"
        )
        assert _field_value(reader, F_16) == _fmt(result.line_16)
        assert _field_value(reader, F_25A) == _fmt(result.line_25a)
        if is_owe:
            assert _field_value(reader, F_37) == _fmt(result.line_37)
        elif is_owe is False:
            assert _field_value(reader, F_34) == _fmt(result.line_34)

        # (4) Correct filing-status checkbox on-value, others Off.
        entry = _STATUS[_STATUS_KEY_FOR[status]]
        got = _field_value(reader, entry["field"])
        assert str(got) == entry["on"], (
            f"[{case_id}] status checkbox {entry['field']} = {got!r}, "
            f"expected {entry['on']}"
        )
        for other, key in _STATUS_KEY_FOR.items():
            if other == status:
                continue
            ov = _field_value(reader, _STATUS[key]["field"])
            assert ov in (None, "/Off") or str(ov) == "/Off", (
                f"[{case_id}] foreign status {other} checkbox set to {ov!r}"
            )


# internal code -> field_spec.json key (Single/MFJ/MFS/HOH/QSS).
_STATUS_KEY_FOR = {
    "single": "Single", "mfj": "MFJ", "mfs": "MFS", "hoh": "HOH", "qss": "QSS",
}


# --------------------------------------------------------------------------- #
# 2. Identity edge cases — long names, apostrophe/hyphen, empty optionals,
#    SSN with separators. Must never crash and must round-trip the name/SSN.
# --------------------------------------------------------------------------- #
_IDENTITY_CASES = {
    "very_long_names": Identity(
        first_name="Maximilian Alexander Bartholomew C",
        last_name="Featherstonehaugh-Wadsworthington",
        ssn="123-45-6789", address="1 Infinite Loop", city="Cupertino",
        state="CA", zip="95014",
    ),
    "apostrophe_hyphen": Identity(
        first_name="D'Angelo", last_name="O'Brien-Smith",
        ssn="123-45-6789", address="12 O'Hare Ave", city="Chicago",
        state="IL", zip="60601",
    ),
    "empty_optional_address": Identity(
        first_name="Pat", last_name="Lee", ssn="123-45-6789",
        # address/city/state/zip all left at their "" defaults
    ),
    "ssn_with_dashes": Identity(
        first_name="Sam", last_name="Park", ssn="123-45-6789",
    ),
}


@pytest.mark.parametrize("flatten", [True, False], ids=["flat", "noflat"])
@pytest.mark.parametrize("ident_key", list(_IDENTITY_CASES), ids=list(_IDENTITY_CASES))
def test_identity_edge_cases(ident_key, flatten):
    ident = _IDENTITY_CASES[ident_key]
    result = _result_refund()
    case_id = f"ident-{ident_key}-{'flat' if flatten else 'noflat'}"
    out = str(_OUTDIR / f"{case_id}.pdf")

    # (1) no exception.
    fill_1040_pdf(result, ident, "single", [], out, flatten=flatten)
    # (2) structural validity.
    reader = _assert_valid_pdf(out, case_id, flatten)

    if not flatten:
        # (4-ish) the name + normalized SSN survive into the AcroForm verbatim.
        assert _field_value(reader, F_FIRST) == ident.first_name, (
            f"[{case_id}] first name not round-tripped"
        )
        assert _field_value(reader, F_LAST) == ident.last_name, (
            f"[{case_id}] last name not round-tripped"
        )
        # SSN supplied as 123-45-6789 -> formatted identically.
        assert _field_value(reader, F_SSN) == "123-45-6789", (
            f"[{case_id}] SSN not formatted 123-45-6789"
        )
        # Empty optional address fields must NOT be written (left absent/blank).
        if ident_key == "empty_optional_address":
            city = _field_value(reader, _SPEC["identity"]["city"])
            assert city in (None, ""), (
                f"[{case_id}] empty city should be unset, got {city!r}"
            )
    else:
        # flattened: the (possibly long / punctuated) last name renders on p1.
        p1 = _page_text(reader, 0)
        # Compare on a punctuation-robust basis: extract_text can drop or shift
        # an apostrophe, so assert the alpha run is present.
        needle = "".join(c for c in ident.last_name if c.isalpha())[:6]
        assert needle.lower() in p1.lower().replace("'", "").replace("-", ""), (
            f"[{case_id}] last name {ident.last_name!r} not found in page text"
        )


# --------------------------------------------------------------------------- #
# 3. Dependents: 0, 1, and 4 (table capacity); mix of CTC child + ODC adult.
#    Proves no crash, the right CTC/ODC box per dependent, and comb-vs-plain
#    SSN handling for the dep-3 comb field.
# --------------------------------------------------------------------------- #
def _dep_child(name: str, ssn: str) -> Dependent:
    return Dependent(name=name, ssn=ssn, relationship="Son",
                     is_under_17=True, has_ssn=True)


def _dep_adult(name: str, ssn: str) -> Dependent:
    return Dependent(name=name, ssn=ssn, relationship="Parent",
                     is_under_17=False, has_ssn=True)


_DEP_SETS = {
    "0_deps": [],
    "1_child": [_dep_child("Sammy Rivera", "987-65-4321")],
    "4_mixed": [
        _dep_child("Kid One", "111-22-3331"),
        _dep_child("Kid Two", "111-22-3332"),
        _dep_child("Kid Three", "111-22-3333"),   # dep 3 -> comb SSN field
        _dep_adult("Granny Adult", "111-22-3334"),  # dep 4 -> ODC
    ],
}


@pytest.mark.parametrize("flatten", [True, False], ids=["flat", "noflat"])
@pytest.mark.parametrize("dep_key", list(_DEP_SETS), ids=list(_DEP_SETS))
def test_dependents_capacity_and_classification(dep_key, flatten):
    deps = _DEP_SETS[dep_key]
    result = _result_refund()
    ident = _base_identity()
    case_id = f"deps-{dep_key}-{'flat' if flatten else 'noflat'}"
    out = str(_OUTDIR / f"{case_id}.pdf")

    # (1) no exception, (2) structural validity.
    fill_1040_pdf(result, ident, "hoh", deps, out, flatten=flatten)
    reader = _assert_valid_pdf(out, case_id, flatten)

    if not flatten and deps:
        spec_rows = _SPEC["dependents"]
        for i, dep in enumerate(deps):
            row = spec_rows[i]
            first = dep.name.split(" ", 1)[0]
            assert _field_value(reader, row["first"]) == first, (
                f"[{case_id}] dep{i + 1} first name mismatch"
            )
            # CTC for under-17-with-SSN, else ODC. Verify the RIGHT box is on
            # and the other is Off.
            ctc_v = _field_value(reader, row["ctc"]["field"])
            odc_v = _field_value(reader, row["odc"]["field"])
            if dep.is_under_17 and dep.has_ssn:
                assert str(ctc_v) == row["ctc"]["on"], (
                    f"[{case_id}] dep{i + 1} CTC box not set"
                )
                assert odc_v in (None, "/Off") or str(odc_v) == "/Off", (
                    f"[{case_id}] dep{i + 1} ODC box wrongly set ({odc_v!r})"
                )
            else:
                assert str(odc_v) == row["odc"]["on"], (
                    f"[{case_id}] dep{i + 1} ODC box not set"
                )
                assert ctc_v in (None, "/Off") or str(ctc_v) == "/Off", (
                    f"[{case_id}] dep{i + 1} CTC box wrongly set ({ctc_v!r})"
                )
            # SSN: dep-3 row is a comb field (digits only); others plain-format.
            if "ssn_COMB9" in row:
                assert _field_value(reader, row["ssn_COMB9"]) == "".join(
                    c for c in dep.ssn if c.isdigit()
                ), f"[{case_id}] dep{i + 1} comb SSN not digits-only"
            else:
                # plain SSN field is formatted 123-45-6789.
                want = dep.ssn if "-" in dep.ssn else dep.ssn
                assert _field_value(reader, row["ssn"]) == want, (
                    f"[{case_id}] dep{i + 1} plain SSN mismatch"
                )


# --------------------------------------------------------------------------- #
# 4. Dedicated zero-case: every key dollar line renders the literal "0" in the
#    unflattened /V (the form's "if zero, -0-" rule), never blank.
# --------------------------------------------------------------------------- #
def test_zero_case_renders_zero_not_blank():
    result = _result_zero()
    ident = _base_identity()
    out = str(_OUTDIR / "zero-explicit.pdf")
    fill_1040_pdf(result, ident, "single", [], out, flatten=False)
    reader = _assert_valid_pdf(out, "zero-explicit", False)
    for label, field in [("1a", F_1A), ("16", F_16), ("25a", F_25A),
                         ("34", F_34), ("37", F_37)]:
        assert _field_value(reader, field) == "0", (
            f"zero-case line_{label} should be '0', got "
            f"{_field_value(reader, field)!r}"
        )


# --------------------------------------------------------------------------- #
# 5. Huge-amount focus: prove thousands commas on 7-AND-8-figure values in BOTH
#    surfaces, and feed the truncation ledger. The flattened render of an
#    8-figure number is the real "field width" stress.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flatten", [True, False], ids=["flat", "noflat"])
def test_huge_amounts_comma_formatting(flatten):
    result = _result_huge()
    ident = _base_identity()
    case_id = f"huge-detail-{'flat' if flatten else 'noflat'}"
    out = str(_OUTDIR / f"{case_id}.pdf")
    fill_1040_pdf(result, ident, "mfj", [], out, flatten=flatten)
    reader = _assert_valid_pdf(out, case_id, flatten)

    # The dollar fields we exercise with big values, paired to their page.
    big = [
        ("line_1a", result.line_1a, F_1A, 0),       # 9,999,999
        ("line_9", result.line_9, _LINES["9_total_income"], 0),   # 12,345,678 (8-fig)
        ("line_16", result.line_16, F_16, 1),        # 3,000,000
        ("line_25a", result.line_25a, F_25A, 1),     # 4,000,000
        ("line_25d", result.line_25d, _LINES["25d_total_withholding"], 1),  # 10,000,000 (8-fig)
        ("line_33", result.line_33, _LINES["33_total_payments"], 1),        # 10,000,000 (8-fig)
        ("line_34", result.line_34, F_34, 1),        # 1,000,000
    ]
    if not flatten:
        # Every big value carries thousands commas in its /V verbatim.
        for label, amount, field, _pg in big:
            assert _field_value(reader, field) == _fmt(amount), (
                f"[{case_id}] {label} /V = {_field_value(reader, field)!r}, "
                f"expected {_fmt(amount)!r} (commas missing or truncated)"
            )
            assert "," in _fmt(amount)  # sanity: these are all >= 1,000,000
    else:
        # Flattened render: record (don't hard-fail) truncation so the report is
        # precise. We DO assert that at least the 7-figure values render fully —
        # if even those clip, that's a real bug and should fail loudly.
        for label, amount, _field, pg in big:
            present = _check_amount_rendered(reader, case_id, label, amount, pg)
            if amount < 10_000_000:  # 7-figure values must render fully.
                assert present, (
                    f"[{case_id}] 7-figure {label}={_fmt(amount)} did NOT render "
                    f"with full commas on page {pg + 1} — genuine truncation/bug"
                )


# --------------------------------------------------------------------------- #
# Final summary — runs last (alphabetically z_) and emits a precise report:
# total cases, the largest fully-rendered amount, and any truncations found.
# This test PASSES as long as no 7-figure value truncated; 8-figure truncation
# is reported as an informational warning, per the task ("report, don't fail").
# --------------------------------------------------------------------------- #
def test_zzz_report_summary():
    n_cases = len(_LEDGER["cases"])
    n_flat = sum(1 for _c, f, _s, _ok in _LEDGER["cases"] if f)
    n_noflat = n_cases - n_flat
    max_full = _LEDGER["max_full_amount"]
    truncs = _LEDGER["truncated"]

    lines = [
        "",
        "=" * 70,
        "PDF ROBUSTNESS REPORT — fill_1040_pdf 'download always works'",
        "=" * 70,
        f"Total PDF generations asserted valid : {n_cases}",
        f"  flatten=True                       : {n_flat}",
        f"  flatten=False                      : {n_noflat}",
        f"Largest amount rendered WITH full commas (flattened): "
        f"${max_full:,}",
    ]
    if truncs:
        lines.append(f"TRUNCATED / MISSING in flattened render: {len(truncs)}")
        for case_id, label, expected, ctx in truncs:
            lines.append(
                f"  - [{case_id}] {label}: expected '{expected}' "
                f"not found; nearby text: {ctx!r}"
            )
        # Group by whether any were 7-figure (genuine bug) vs 8-figure (width).
        # t[2] is the expected comma-formatted string; strip commas to count digits.
        seven_fig = [t for t in truncs if len(t[2].replace(",", "")) <= 7]
        if seven_fig:
            lines.append(
                "  !! At least one <=7-figure value truncated — GENUINE BUG."
            )
        else:
            lines.append(
                "  (All truncations are 8-figure values — field-width limit, "
                "reported per task; not a crash.)"
            )
    else:
        lines.append(
            "No truncation detected: every dollar value tested (incl. 8-figure) "
            "rendered fully with thousands commas."
        )
    lines.append("=" * 70)
    lines.append(f"Artifacts written to: {_OUTDIR}")
    lines.append("=" * 70)
    report = "\n".join(lines)
    print(report)

    # Hard gate: NO 7-figure (or smaller) value may truncate — that would mean
    # download is broken for realistic amounts. 8-figure truncation is allowed
    # through as an informational finding.
    seven_fig_truncs = [
        t for t in truncs if len(t[2].replace(",", "")) <= 7
    ]
    assert not seven_fig_truncs, (
        "Download/fill produced a TRUNCATED 7-figure (or smaller) dollar value:\n"
        + "\n".join(f"  [{c}] {lbl}: expected {exp!r}" for c, lbl, exp, _ in
                    seven_fig_truncs)
    )
