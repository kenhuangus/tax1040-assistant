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
    "commas). If the input is not a W-2 or is missing Box 1 or Box 2, call the "
    "tool with your best effort but leave unknown numeric fields as 0. Always "
    "call the emit_w2 tool; do not answer in prose."
)


def _coerce_money(value: Any) -> int:
    """Coerce a money-ish value (``"40,000"``, ``"$40000.00"``, ``40000``) to int."""
    if value is None:
        raise ValueError("missing money value")
    if isinstance(value, bool):  # guard: bools are ints in Python
        raise ValueError("invalid money value")
    if isinstance(value, (int, float)):
        return int(round(value))
    s = str(value).strip().replace("$", "").replace(",", "")
    if s == "":
        raise ValueError("empty money value")
    return int(round(float(s)))


def _build_w2(d: dict) -> W2:
    """Validate a raw field dict into the frozen ``W2`` model.

    Raises ``ValueError`` on anything malformed so the guardrail layer logs a
    schema hit rather than letting bad facts into a slot.
    """
    try:
        wages = _coerce_money(d.get("wages"))
        fed = _coerce_money(d.get("fed_withholding", d.get("federalTaxWithheld")))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"W-2 money fields invalid: {exc}") from exc

    if wages < 0 or fed < 0:
        raise ValueError("W-2 money fields must be non-negative whole dollars")

    name = (d.get("employee_name") or d.get("name") or "").strip()
    ssn = (d.get("ssn") or "").strip()
    if not name:
        raise ValueError("W-2 is missing the employee name")
    if not ssn:
        raise ValueError("W-2 is missing the employee SSN")

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
