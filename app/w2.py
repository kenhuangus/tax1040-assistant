"""
W-2 extraction tool — text or image → validated ``W2`` (Pydantic).

``parse_w2(raw_text=None, image_b64=None)`` uses the LLM to extract the W-2
fields, then validates the result into the frozen ``W2`` model (``app.models``).
On bad / partial / non-W-2 data it raises ``ValueError`` so the guardrail layer
catches it and records a ``schema`` guardrail hit (R3.1).

Box 1 → ``wages``, Box 2 → ``fed_withholding`` (whole dollars). Identity (name,
SSN, address) also comes from the W-2 so the conversation needs no extra
questions for it.

A deterministic fast-path handles a pasted JSON W-2 (the "load sample" button
posts JSON-ish text) without burning an LLM call, which also keeps the demo and
no-key local runs working.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.models import W2

# Tool schema for the LLM extraction sub-call. Strict so the model returns
# clean whole-dollar ints and the identity fields we need for the PDF.
_EXTRACT_TOOL = {
    "name": "emit_w2",
    "description": "Emit the structured fields extracted from a W-2 form.",
    "input_schema": {
        "type": "object",
        "properties": {
            "employee_name": {"type": "string", "description": "Box e: employee's full name"},
            "ssn": {"type": "string", "description": "Box a: employee SSN, formatted 123-45-6789"},
            "address": {"type": "string", "description": "Box f: street address"},
            "city": {"type": "string"},
            "state": {"type": "string", "description": "2-letter state code"},
            "zip": {"type": "string"},
            "employer": {"type": "string", "description": "Box c: employer name"},
            "wages": {"type": "integer", "description": "Box 1: wages, tips, other comp (whole dollars)"},
            "fed_withholding": {"type": "integer", "description": "Box 2: federal income tax withheld (whole dollars)"},
        },
        "required": ["employee_name", "ssn", "wages", "fed_withholding"],
    },
}

_EXTRACT_SYSTEM = (
    "You extract fields from a U.S. W-2 wage statement. Return whole-dollar "
    "integers for the money boxes (round to the nearest dollar; strip $ and "
    "commas). CRITICAL: never fabricate or guess a value. If you genuinely "
    "cannot read a box, OMIT that field entirely from the tool call — do NOT "
    "emit 0 as a placeholder for an unreadable box (0 means a real, legible "
    "zero, e.g. Box 2 federal tax withheld can legitimately be 0). Likewise "
    "omit employee_name or ssn if you cannot read them rather than inventing "
    "them. If the input is clearly not a W-2, still call the tool but omit the "
    "fields you cannot find. Always call the emit_w2 tool; do not answer in prose."
)


def _coerce_money(value: Any) -> int:
    """Coerce a money-ish value (``"40,000"``, ``"$40000.00"``, ``40000``) to int."""
    if value is None:
        raise ValueError("missing money value")
    if isinstance(value, bool):  # guard: bools are ints in Python
        raise ValueError("invalid money value")
    if isinstance(value, (int, float)):
        return int(round(value))
    s = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if s == "":
        raise ValueError("empty money value")
    return int(round(float(s)))


def _coerce_ssn(value: Any) -> str:
    """Normalize an SSN-ish value to ``123-45-6789``.

    Accepts ``"123-45-6789"``, ``"123456789"``, spaced variants, etc. Anything
    without 9 digits is rejected so the caller can ask for a valid SSN.
    """
    if value is None:
        raise ValueError("missing SSN")
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 9:
        raise ValueError("SSN does not contain 9 digits")
    return f"{digits[0:3]}-{digits[3:5]}-{digits[5:9]}"


# Keys that signal the dict actually came from a W-2 (vs arbitrary JSON).
_W2_SIGNAL_KEYS = (
    "wages",
    "fed_withholding",
    "federalTaxWithheld",
    "employee_name",
    "name",
    "ssn",
    "employer",
)


def _looks_like_w2(d: dict) -> bool:
    """True if the dict carries at least one recognizable W-2 field with a value.

    Guards against arbitrary JSON (e.g. ``{"foo": "bar"}``) being treated as a
    partial W-2 — that should surface the friendly "doesn't look like a W-2"
    message rather than a field-specific one.
    """
    for k in _W2_SIGNAL_KEYS:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return True
    return False


def _build_w2(d: dict) -> W2:
    """Validate a raw field dict into the frozen ``W2`` model.

    Raises ``ValueError`` on anything malformed so the guardrail layer logs a
    schema hit rather than letting bad facts into a slot.
    """
    # If essentially nothing recognizable came through, it isn't a W-2 at all.
    if not _looks_like_w2(d):
        raise ValueError(
            "This doesn't look like a W-2 — I couldn't find wages, a name, or an "
            "SSN in it. Could you paste your W-2 (or its Box 1 wages, Box 2 "
            "federal tax withheld, your name, and SSN)?"
        )

    # --- Box 1 wages: missing/unreadable -> treat as MISSING, never $0 -----
    raw_wages = d.get("wages")
    if raw_wages is None or (isinstance(raw_wages, str) and raw_wages.strip() == ""):
        raise ValueError(
            "I couldn't read your wages (Box 1) — what was the amount in Box 1?"
        )
    try:
        wages = _coerce_money(raw_wages)
    except (ValueError, TypeError):
        raise ValueError(
            "I couldn't read your wages (Box 1) — what was the amount in Box 1?"
        ) from None
    if wages <= 0:
        # A $0-wage W-2 means Box 1 was unreadable/missing for this tool.
        raise ValueError(
            "I couldn't read your wages (Box 1) — what was the amount in Box 1?"
        )
    if wages < 0:  # pragma: no cover - unreachable, kept explicit for non-neg rule
        raise ValueError("Box 1 wages can't be negative — what was the amount in Box 1?")

    # --- Box 2 federal tax withheld: 0 is LEGITIMATE; only reject if present
    #     and unparseable. Absent -> default 0 (some W-2s omit it).
    raw_fed = d.get("fed_withholding", d.get("federalTaxWithheld"))
    if raw_fed is None or (isinstance(raw_fed, str) and raw_fed.strip() == ""):
        fed = 0
    else:
        try:
            fed = _coerce_money(raw_fed)
        except (ValueError, TypeError):
            raise ValueError(
                "I couldn't read your federal tax withheld (Box 2) — what was the "
                "amount in Box 2? (Enter 0 if nothing was withheld.)"
            ) from None
    if fed < 0:
        raise ValueError(
            "Box 2 federal tax withheld can't be negative — what was the amount in "
            "Box 2? (Enter 0 if nothing was withheld.)"
        )

    name = (d.get("employee_name") or d.get("name") or "").strip()
    if not name:
        raise ValueError(
            "I couldn't find the employee name on this W-2 — what name is on it "
            "(Box e)?"
        )

    try:
        ssn = _coerce_ssn(d.get("ssn"))
    except (ValueError, TypeError):
        raise ValueError(
            "I couldn't read a valid Social Security number (Box a) — what's the "
            "9-digit SSN (e.g. 123-45-6789)?"
        ) from None

    try:
        # Pydantic does the final schema enforcement (types, required fields).
        return W2(
            employee_name=name,
            ssn=ssn,
            address=(d.get("address") or "").strip(),
            city=(d.get("city") or "").strip(),
            state=(d.get("state") or "").strip(),
            zip=str(d.get("zip") or "").strip(),
            employer=(d.get("employer") or "").strip(),
            wages=wages,
            fed_withholding=fed,
        )
    except Exception as exc:  # pydantic ValidationError -> surface as ValueError
        raise ValueError(f"W-2 failed schema validation: {exc}") from exc


def _try_json(raw_text: str) -> Optional[dict]:
    """Best-effort: extract a JSON object out of the pasted text (sample loader)."""
    raw_text = raw_text.strip()
    # Direct parse first.
    try:
        obj = json.loads(raw_text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # Embedded JSON object anywhere in the message.
    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def parse_w2(raw_text: str | None = None, image_b64: str | None = None) -> W2:
    """Extract a W-2 from pasted text or a base64 image into a validated ``W2``.

    Strategy:
      1. If ``raw_text`` parses as a JSON object (the sample-W-2 loader, or any
         structured paste), validate it directly — deterministic, no LLM call.
      2. Otherwise call the LLM (``emit_w2`` tool) on the text and/or image.

    Raises ``ValueError`` on bad / partial / non-W-2 data (caught by the
    guardrail layer, surfaced as a schema guardrail hit).
    """
    if not raw_text and not image_b64:
        raise ValueError("parse_w2 needs raw_text or image_b64")

    # --- deterministic JSON fast-path -------------------------------------
    if raw_text:
        obj = _try_json(raw_text)
        if obj is not None:
            # Map the NovaTax-style sample keys onto our W2 fields if present.
            normalized = dict(obj)
            if "federalTaxWithheld" in obj and "fed_withholding" not in obj:
                normalized["fed_withholding"] = obj["federalTaxWithheld"]
            return _build_w2(normalized)

    # --- LLM extraction ----------------------------------------------------
    from app.llm import llm_turn  # lazy: keep w2 importable without a key

    content: list[dict] = []
    if image_b64:
        # Accept either a bare base64 string or a data URL.
        media_type = "image/png"
        b64 = image_b64
        m = re.match(r"data:(image/[\w.+-]+);base64,(.*)", image_b64, re.DOTALL)
        if m:
            media_type = m.group(1)
            b64 = m.group(2)
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            }
        )
    if raw_text:
        content.append({"type": "text", "text": raw_text})
    if not content:  # pragma: no cover - guarded above
        raise ValueError("parse_w2 needs raw_text or image_b64")

    messages = [{"role": "user", "content": content}]
    tool_calls, _ = llm_turn(_EXTRACT_SYSTEM, messages, [_EXTRACT_TOOL])

    emit = next((tc for tc in tool_calls if tc.name == "emit_w2"), None)
    if emit is None:
        raise ValueError("Could not read a W-2 from the provided input.")

    args = dict(emit.args)
    args.pop("_tool_use_id", None)
    return _build_w2(args)


__all__ = ["parse_w2"]
