"""
Fill the official 2025 IRS Form 1040 AcroForm from a computed result.

Source of truth for every field id + checkbox on-value is the sentinel-verified
``assets/irs/_probe/field_spec.json`` (read at runtime so it stays canonical).
Render-safe recipe per ``docs/architecture.md`` section 5: clone_from=reader to keep
the /DR fonts, drop the hybrid /XFA, write per-page field values with
auto_regenerate=False, set NeedAppearances, and (when flatten=True) bake the
appearance streams + strip widgets + delete /AcroForm so the values render
identically in Chrome / PDFium.
"""
from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

from app.models import Dependent, Form1040Result, Identity

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file so cwd never matters)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
TEMPLATE_PDF = _REPO / "assets" / "irs" / "f1040--2025.pdf"
FIELD_SPEC = _REPO / "assets" / "irs" / "_probe" / "field_spec.json"


def _load_spec() -> dict:
    """Load the verified field map (the source of truth)."""
    with open(FIELD_SPEC, "r", encoding="utf-8") as fh:
        return json.load(fh)


# Map our internal filing-status code -> the key used in field_spec.json.
_STATUS_KEY = {
    "single": "Single",
    "mfj": "MFJ",
    "mfs": "MFS",
    "hoh": "HOH",
    "qss": "QSS",
}

# Map a Form1040Result attribute -> the field_spec.json "lines_dollar" key.
# These are the ~15 dollar lines the assistant populates. line_11 (AGI) appears
# on both page 1 and page 2, so it maps to two fields.
_DOLLAR_LINES: list[tuple[str, str]] = [
    ("line_1a", "1a_wages"),
    ("line_1z", "1z_total_wages"),
    ("line_9", "9_total_income"),
    ("line_11", "11_AGI_page1"),
    ("line_11", "11b_AGI_page2"),
    ("line_12", "12_std_deduction"),
    ("line_15", "15_taxable_income"),
    ("line_16", "16_tax"),
    ("line_19", "19_CTC"),
    ("line_22", "22"),
    ("line_24", "24_total_tax"),
    ("line_25a", "25a_W2_withholding"),
    ("line_25d", "25d_total_withholding"),
    ("line_33", "33_total_payments"),
    ("line_34", "34_overpaid_refund"),
    ("line_35a", "35a_refunded"),
    ("line_37", "37_amount_owed"),
]


def _fmt_money(amount: int) -> str:
    """Whole-dollar int -> string with thousands commas. Zero renders as "0".

    The form is whole-dollar; negatives (shouldn't happen for our profile) are
    formatted with a leading minus so they are at least visible rather than lost.
    """
    n = int(amount)
    if n < 0:
        return "-" + f"{-n:,}"
    return f"{n:,}"


def _digits_only(s: str) -> str:
    """Strip everything but digits (comb fields take digits, no separators)."""
    return "".join(ch for ch in str(s) if ch.isdigit())


def _fmt_ssn(ssn: str) -> str:
    """Format a 9-digit SSN as 123-45-6789 for the PLAIN-text primary SSN field.

    If the input isn't exactly 9 digits, return it unchanged (best effort — the
    form is still filled, just not reformatted).
    """
    d = _digits_only(ssn)
    if len(d) == 9:
        return f"{d[0:3]}-{d[3:5]}-{d[5:9]}"
    return str(ssn)


def _build_values(
    result: Form1040Result,
    identity: Identity,
    filing_status: str,
    dependents: list[Dependent],
    spec: dict,
) -> dict[str, str]:
    """Build the {fully_qualified_field_name: value} dict to write."""
    values: dict[str, str] = {}

    ident = spec["identity"]
    # ---- identity ----
    if identity.first_name:
        values[ident["your_first_mi"]] = identity.first_name
    if identity.last_name:
        values[ident["your_last"]] = identity.last_name
    if identity.ssn:
        # Primary SSN is a PLAIN text field -> format 123-45-6789 ourselves.
        values[ident["your_ssn"]] = _fmt_ssn(identity.ssn)
    if identity.address:
        values[ident["address_street"]] = identity.address
    if identity.city:
        values[ident["city"]] = identity.city
    if identity.state:
        values[ident["state"]] = identity.state
    if identity.zip:
        values[ident["zip"]] = identity.zip

    # ---- filing status (exactly one checkbox; on-value carries leading slash) ----
    status_key = _STATUS_KEY.get((filing_status or "").lower())
    if status_key:
        entry = spec["filing_status_ONEOF"][status_key]
        values[entry["field"]] = entry["on"]

    # ---- dollar lines ----
    for attr, spec_key in _DOLLAR_LINES:
        amount = int(getattr(result, attr, 0) or 0)
        field = spec["lines_dollar"][spec_key]
        values[field] = _fmt_money(amount)

    # ---- dependents (up to 4 rows on the form) ----
    dep_rows = spec["dependents"]
    for i, dep in enumerate(dependents[: len(dep_rows)]):
        row = dep_rows[i]
        first, _, last = dep.name.partition(" ")
        if first:
            values[row["first"]] = first
        if last:
            values[row["last"]] = last
        if dep.ssn:
            # dep 3's SSN is a comb field (digits only); the rest are plain text.
            ssn_field = row.get("ssn") or row.get("ssn_COMB9")
            if ssn_field:
                if "ssn_COMB9" in row:
                    values[ssn_field] = _digits_only(dep.ssn)
                else:
                    values[ssn_field] = _fmt_ssn(dep.ssn)
        if dep.relationship:
            values[row["relationship"]] = dep.relationship
        # CTC vs ODC box: child tax credit if under 17 & has SSN, else credit
        # for other dependents.
        if dep.is_under_17 and dep.has_ssn:
            box = row["ctc"]
        else:
            box = row["odc"]
        values[box["field"]] = box["on"]

    return values


def _apply_to_pages(writer: PdfWriter, values: dict[str, str], flatten: bool) -> None:
    """Write the field values onto every page (fields are page-scoped in pypdf)."""
    for page in writer.pages:
        writer.update_page_form_field_values(
            page, values, auto_regenerate=False, flatten=flatten
        )


def _flatten_writer(writer: PdfWriter) -> None:
    """Bake values in: strip /Widget annotations and delete /AcroForm.

    After appearances are written (flatten=True on update_page_form_field_values),
    removing the interactive widgets + AcroForm leaves a static PDF whose values
    render identically in every viewer (Chrome included).
    """
    for page in writer.pages:
        if "/Annots" not in page:
            continue
        annots = page["/Annots"]
        keep = []
        for ref in annots:
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Widget":
                continue  # drop interactive form widgets (now baked into content)
            keep.append(ref)
        if keep:
            page[NameObject("/Annots")] = writer._add_object_array(keep) if hasattr(
                writer, "_add_object_array"
            ) else annots.__class__(keep)
        else:
            del page[NameObject("/Annots")]

    root = writer._root_object
    if "/AcroForm" in root:
        del root[NameObject("/AcroForm")]


def fill_1040_pdf(
    result: Form1040Result,
    identity: Identity,
    filing_status: str,
    dependents: list[Dependent],
    out_path: str,
    flatten: bool = True,
) -> str:
    """Fill the 2025 Form 1040 template and write it to ``out_path``.

    Parameters
    ----------
    result : Form1040Result
        Computed line values (whole-dollar ints).
    identity : Identity
        Taxpayer name / SSN / address for the header.
    filing_status : str
        One of single|mfj|mfs|hoh|qss (checkbox set with the verified on-value).
    dependents : list[Dependent]
        Up to four are written to the dependents table; extras are ignored.
    out_path : str
        Destination path for the filled PDF.
    flatten : bool, default True
        When True, bake appearances + strip widgets + delete /AcroForm so values
        render identically in Chrome/PDFium. When False, leave the form
        interactive (still renders via per-widget /AP streams).

    Returns
    -------
    str
        ``out_path`` (the file that was written).
    """
    spec = _load_spec()
    values = _build_values(result, identity, filing_status, dependents, spec)

    reader = PdfReader(str(TEMPLATE_PDF))
    writer = PdfWriter(clone_from=reader)  # clone keeps the /DR fonts + appearances

    # Drop the hybrid XFA so static AcroForm values are authoritative everywhere.
    acro = writer._root_object.get("/AcroForm")
    if acro is not None:
        acro = acro.get_object()
        if "/XFA" in acro:
            del acro[NameObject("/XFA")]

    _apply_to_pages(writer, values, flatten=flatten)

    # Helps Acrobat regenerate appearances; harmless / irrelevant for Chrome.
    writer.set_need_appearances_writer(True)

    if flatten:
        _flatten_writer(writer)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        writer.write(fh)

    return out_path
