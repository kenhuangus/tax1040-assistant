"""
Demo: fill the 2025 Form 1040 for the canonical single / $40k case, write the
flattened deliverable to ``out/1040_demo.pdf``, then re-read it and prove the key
values (wages, withholding, refund) are present.

Run:
    python scripts/fill_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a script (python scripts/fill_demo.py).
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from pypdf import PdfReader

from app.models import Form1040Result, Identity
from app.pdf_fill import fill_1040_pdf


def build_demo_result() -> Form1040Result:
    """The golden single / $40k wages / $3,000 withheld case."""
    return Form1040Result(
        line_1a=40000,
        line_1z=40000,
        line_9=40000,
        line_11=40000,
        line_12=15750,
        line_14=15750,
        line_15=24250,
        line_16=2675,
        line_18=2675,
        line_22=2675,
        line_24=2675,
        line_25a=3000,
        line_25d=3000,
        line_33=3000,
        line_34=325,
        line_35a=325,
        refund=325,
    )


def build_demo_identity() -> Identity:
    return Identity(
        first_name="Jordan A",
        last_name="Rivera",
        ssn="123-45-6789",
        address="742 Evergreen Terrace",
        city="Springfield",
        state="IL",
        zip="62704",
    )


def main() -> int:
    result = build_demo_result()
    identity = build_demo_identity()

    out_dir = _REPO / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / "1040_demo.pdf")

    # Flattened deliverable: values are baked into the page content so they
    # render identically in Chrome / PDFium.
    written = fill_1040_pdf(
        result, identity, "single", [], out_path, flatten=True
    )
    abs_written = str(Path(written).resolve())

    # Re-read the written PDF and prove the values are present. After flattening
    # there is no AcroForm, so the values live in the page content stream and are
    # recovered via text extraction (exactly what a PDF viewer renders).
    reader = PdfReader(abs_written)
    page1_text = reader.pages[0].extract_text() or ""
    page2_text = reader.pages[1].extract_text() or ""

    readback = {
        "name (page 1)": "Jordan" if "Jordan" in page1_text else "<MISSING>",
        "ssn (page 1)": "123-45-6789" if "123-45-6789" in page1_text else "<MISSING>",
        "line_1a wages (page 1)": "40,000" if "40,000" in page1_text else "<MISSING>",
        "line_15 taxable (page 2)": "24,250" if "24,250" in page2_text else "<MISSING>",
        "line_16 tax (page 2)": "2,675" if "2,675" in page2_text else "<MISSING>",
        "line_25a withholding (page 2)": "3,000" if "3,000" in page2_text else "<MISSING>",
        "line_34 refund (page 2)": "325" if "325" in page2_text else "<MISSING>",
    }

    print(f"Wrote filled 1040 -> {abs_written}")
    print(f"File size: {Path(abs_written).stat().st_size:,} bytes, pages: {len(reader.pages)}")
    print("Read-back values (from flattened content):")
    for label, value in readback.items():
        print(f"  {label:32s} {value}")

    # Hard assertions on the three values the requirement calls out.
    assert "40,000" in page1_text, "wages (line 1a) not found in re-read PDF"
    assert "3,000" in page2_text, "withholding (line 25a) not found in re-read PDF"
    assert "325" in page2_text, "refund (line 34) not found in re-read PDF"
    print("OK: wages, withholding, and refund all present in the written PDF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
