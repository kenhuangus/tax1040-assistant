"""
Tests for app.pdf_fill.fill_1040_pdf.

Strategy: fill once WITHOUT flattening so the AcroForm survives and we can read
exact field /V values + checkbox state; fill once WITH flattening to assert the
deliverable is a non-trivial, valid PDF whose values are baked into page content.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

# Make the repo root importable when pytest is invoked from anywhere.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.models import Dependent, Form1040Result, Identity
from app.pdf_fill import fill_1040_pdf

# Fully-qualified field ids (from the verified field_spec.json).
F_FIRST = "topmostSubform[0].Page1[0].f1_11[0]"
F_LAST = "topmostSubform[0].Page1[0].f1_12[0]"
F_SSN = "topmostSubform[0].Page1[0].f1_13[0]"
F_SINGLE_CB = "topmostSubform[0].Page1[0].Checkbox_ReadOrder[0].c1_8[0]"
F_MFJ_CB = "topmostSubform[0].Page1[0].Checkbox_ReadOrder[0].c1_8[1]"
F_1A = "topmostSubform[0].Page1[0].f1_47[0]"
F_15 = "topmostSubform[0].Page2[0].f2_06[0]"
F_16 = "topmostSubform[0].Page2[0].f2_08[0]"
F_25A = "topmostSubform[0].Page2[0].f2_17[0]"
F_34 = "topmostSubform[0].Page2[0].f2_30[0]"
F_DEP1_FIRST = "topmostSubform[0].Page1[0].Table_Dependents[0].Row1[0].f1_31[0]"
F_DEP1_CTC = "topmostSubform[0].Page1[0].Table_Dependents[0].Row7[0].Dependent1[0].c1_28[0]"


def _golden_result() -> Form1040Result:
    return Form1040Result(
        line_1a=40000, line_1z=40000, line_9=40000, line_11=40000,
        line_12=15750, line_14=15750, line_15=24250, line_16=2675,
        line_18=2675, line_22=2675, line_24=2675, line_25a=3000,
        line_25d=3000, line_33=3000, line_34=325, line_35a=325, refund=325,
    )


def _identity() -> Identity:
    return Identity(
        first_name="Jordan A", last_name="Rivera", ssn="123456789",
        address="742 Evergreen Terrace", city="Springfield", state="IL", zip="62704",
    )


def _field_value(reader: PdfReader, name: str):
    fields = reader.get_fields()
    f = fields.get(name)
    return None if f is None else f.get("/V")


def test_unflattened_field_values(tmp_path):
    """With the AcroForm intact, exact field /V values and checkbox are set."""
    out = str(tmp_path / "1040_unflat.pdf")
    ret = fill_1040_pdf(_golden_result(), _identity(), "single", [], out, flatten=False)
    assert ret == out

    reader = PdfReader(out)

    assert _field_value(reader, F_FIRST) == "Jordan A"
    assert _field_value(reader, F_LAST) == "Rivera"
    # Primary SSN must be PLAIN text formatted 123-45-6789.
    assert _field_value(reader, F_SSN) == "123-45-6789"

    # Dollar lines carry thousands commas.
    assert _field_value(reader, F_1A) == "40,000"
    assert _field_value(reader, F_15) == "24,250"
    assert _field_value(reader, F_16) == "2,675"
    assert _field_value(reader, F_25A) == "3,000"
    assert _field_value(reader, F_34) == "325"


def test_filing_status_checkbox_single(tmp_path):
    """Single -> the Single checkbox is /1 and MFJ is left Off."""
    out = str(tmp_path / "1040_single.pdf")
    fill_1040_pdf(_golden_result(), _identity(), "single", [], out, flatten=False)
    reader = PdfReader(out)

    single = _field_value(reader, F_SINGLE_CB)
    mfj = _field_value(reader, F_MFJ_CB)
    # Value may be a NameObject; compare by string.
    assert str(single) == "/1"
    assert mfj in (None, "/Off") or str(mfj) == "/Off"


@pytest.mark.parametrize(
    "status,field,on",
    [
        ("single", F_SINGLE_CB, "/1"),
        ("mfj", F_MFJ_CB, "/2"),
    ],
)
def test_filing_status_parametrized(tmp_path, status, field, on):
    out = str(tmp_path / f"1040_{status}.pdf")
    fill_1040_pdf(_golden_result(), _identity(), status, [], out, flatten=False)
    reader = PdfReader(out)
    assert str(_field_value(reader, field)) == on


def test_zero_renders_as_zero(tmp_path):
    """A zero dollar line renders as the string '0', not blank."""
    res = Form1040Result(line_1a=0, line_25a=0)
    out = str(tmp_path / "1040_zero.pdf")
    fill_1040_pdf(res, _identity(), "single", [], out, flatten=False)
    reader = PdfReader(out)
    assert _field_value(reader, F_1A) == "0"
    assert _field_value(reader, F_25A) == "0"


def test_dependents_written(tmp_path):
    """A dependent populates row 1 and the CTC box (under 17, has SSN)."""
    deps = [Dependent(name="Sammy Rivera", ssn="987654321",
                       relationship="Son", is_under_17=True, has_ssn=True)]
    out = str(tmp_path / "1040_dep.pdf")
    fill_1040_pdf(_golden_result(), _identity(), "single", deps, out, flatten=False)
    reader = PdfReader(out)
    assert _field_value(reader, F_DEP1_FIRST) == "Sammy"
    assert str(_field_value(reader, F_DEP1_CTC)) == "/1"


def test_flattened_is_valid_nontrivial_pdf(tmp_path):
    """The flattened deliverable is a valid 2-page PDF with no AcroForm and the
    values baked into page content (what a viewer renders)."""
    out = str(tmp_path / "1040_flat.pdf")
    fill_1040_pdf(_golden_result(), _identity(), "single", [], out, flatten=True)

    data = Path(out).read_bytes()
    assert data[:5] == b"%PDF-", "not a PDF header"
    assert len(data) > 50_000, "flattened PDF unexpectedly small"

    reader = PdfReader(out)
    assert len(reader.pages) == 2
    # AcroForm should be gone after flattening.
    assert "/AcroForm" not in reader.trailer["/Root"]

    page1 = reader.pages[0].extract_text() or ""
    page2 = reader.pages[1].extract_text() or ""
    assert "40,000" in page1
    assert "Jordan" in page1
    assert "24,250" in page2
    assert "2,675" in page2
    assert "325" in page2
