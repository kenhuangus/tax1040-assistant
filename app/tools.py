"""
P2 — Tools: the registry of typed tool schemas + the SINGLE dispatch path.

This is pillar 2, and ``dispatch`` is the BLOCKER invariant: it is the ONLY code
in the whole system that mutates session slots, triggers compute, or fills the
PDF. The orchestrator reads ONLY ``tool_use`` blocks from the model and routes
each one through ``dispatch``. It never scrapes numbers from assistant prose.

Tools:
  * ``parse_w2``      — text/image -> validated W2 -> sets the w2 slot (+ identity).
  * ``set_slot``      — set one schema-validated slot (filing_status / dependents /
                        confirm).
  * ``compute_1040``  — run the deterministic tax core -> sets session.result
                        (unlocks download).
  * ``fill_1040_pdf`` — render the official IRS AcroForm PDF -> sets pdf_path.
  * ``correct_slot``  — (stretch) overwrite a prior answer WITHOUT consuming a
                        question; clears downstream result/pdf so they recompute.

``dispatch`` returns a result dict AND records ``slot_changes`` / ``GuardrailHit``
into a per-call scratch the orchestrator drains into the turn Event. To keep the
ToolCall schema clean, dispatch attaches its outcome onto the passed-in
``tool_call.result`` and returns a structured dict.

Imports of ``app.compute`` / ``app.pdf_fill`` are LAZY (inside the dispatch
branches) so ``import app.tools`` (and ``app.main``) succeed even while agents A
and B are still writing those files.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

from app.models import Dependent, Form1040Result, GuardrailHit, Identity, TaxFacts, ToolCall, W2
from app.statemachine import VALID_FILING_STATUSES

# ---------------------------------------------------------------------------
# Tool schemas (anthropic tool-use format). Descriptions steer the model; the
# code in dispatch is what actually enforces validity.
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "name": "parse_w2",
        "description": (
            "Extract the user's W-2 from pasted text or an uploaded image. Call "
            "this when the user provides or pastes a W-2. Sets the wages, "
            "federal withholding, and identity (name, SSN, address) from the form."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_text": {"type": "string", "description": "Pasted W-2 text (or JSON)."},
                "image_b64": {"type": "string", "description": "Base64-encoded W-2 image."},
            },
        },
    },
    {
        "name": "set_slot",
        "description": (
            "Save ONE validated answer to the return.\n"
            "- name='filing_status': value is one of single|mfj|mfs|hoh|qss.\n"
            "- name='dependents': value is a JSON list of dependent OBJECTS (or an "
            "empty list / 0 / 'none' if the user has none). Each dependent object "
            "MUST be: {\"name\": str, \"ssn\": str, \"relationship\": str, "
            "\"is_under_17\": bool, \"has_ssn\": bool}. Set is_under_17=true when "
            "the dependent was UNDER AGE 17 at the end of 2025 (this is what makes "
            "them a qualifying child for the $2,200 Child Tax Credit instead of the "
            "$500 Credit for Other Dependents) and false otherwise; set has_ssn=true "
            "if the dependent has a Social Security Number. Always fill is_under_17 "
            "explicitly from the user's facts (e.g. their age) — do not omit it.\n"
            "- name='confirm': value true when the user approves the summary so the "
            "return can be computed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["filing_status", "dependents", "confirm"],
                },
                "value": {
                    "description": (
                        "The value for the slot. For filing_status: a status string. "
                        "For confirm: true/false. For dependents: a JSON array of "
                        "objects, each {name, ssn, relationship, is_under_17 (boolean: "
                        "true if the dependent was under age 17 at the end of 2025), "
                        "has_ssn (boolean)} — or an empty list / 0 / 'none' for no "
                        "dependents."
                    ),
                    "type": ["array", "string", "number", "boolean", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Dependent's full name."},
                            "ssn": {"type": "string", "description": "Dependent's SSN (verbatim)."},
                            "relationship": {
                                "type": "string",
                                "description": "Relationship to the filer (e.g. son, daughter, parent).",
                            },
                            "is_under_17": {
                                "type": "boolean",
                                "description": "True if the dependent was under age 17 at the end of 2025 (qualifying child for the Child Tax Credit).",
                            },
                            "has_ssn": {
                                "type": "boolean",
                                "description": "True if the dependent has a Social Security Number.",
                            },
                        },
                        "required": ["name", "is_under_17"],
                    },
                },
            },
            "required": ["name", "value"],
        },
    },
    {
        "name": "compute_1040",
        "description": (
            "Run the deterministic 1040 tax calculation from the collected facts. "
            "Call this only after filing status, the W-2, and dependents are set "
            "and the user has confirmed. Produces all line values and unlocks the "
            "PDF download."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fill_1040_pdf",
        "description": (
            "Fill the official IRS Form 1040 PDF with the computed result. Call "
            "after compute_1040 has succeeded. Produces the downloadable return."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "correct_slot",
        "description": (
            "Change a previously-given answer (filing_status or dependents) "
            "WITHOUT counting a new question. Use when the user wants to fix an "
            "earlier answer. Clears any computed result so it recomputes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": ["filing_status", "dependents"]},
                "value": {"description": "The corrected value."},
            },
            "required": ["name", "value"],
        },
    },
]

# Quick lookup of tool names we accept.
TOOL_NAMES = {t["name"] for t in TOOLS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Valid fields on the Dependent model. Anything else a model emits (age,
# qualifying_child, etc.) is interpreted into these and then dropped.
_DEPENDENT_FIELDS = ("name", "ssn", "relationship", "is_under_17", "has_ssn")

# Phrasings a model commonly emits in an `age` field to mean "younger than 17".
_UNDER_17_PHRASES = {
    "under 17", "under17", "under-17", "<17", "< 17", "under age 17",
    "younger than 17", "minor", "child",
}


def _truthy(value: Any) -> bool:
    """Loose truth test for model-emitted booleans (handles "true"/"yes"/1)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "t")
    return bool(value)


def _age_under_17(value: Any) -> bool | None:
    """Interpret an age-ish value. Returns True/False if decidable, else None.

    Accepts ints (16 -> True, 18 -> False), numeric strings ("16"), and free
    text like "under 17" / "under17" / "16 years old".
    """
    if isinstance(value, bool):  # guard: bool is a subclass of int
        return None
    if isinstance(value, (int, float)):
        return value < 17
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            return None
        if s in _UNDER_17_PHRASES or "under 17" in s or "under17" in s:
            return True
        # Pull the first integer out of strings like "16", "16 years", "age 9".
        import re

        m = re.search(r"\d+", s)
        if m:
            return int(m.group()) < 17
    return None


def _derive_is_under_17(item: dict) -> bool:
    """Derive ``is_under_17`` from the many shapes a model emits.

    Honors an explicit ``is_under_17`` first; otherwise infers from age /
    under_17 / is_child / qualifying_child / a child-ish relationship paired
    with an age<17 signal. Defaults False (-> $500 ODC) when nothing indicates a
    qualifying child.
    """
    if "is_under_17" in item and item["is_under_17"] is not None:
        return _truthy(item["is_under_17"])

    # Direct age signals.
    for key in ("age", "age_years", "age_at_year_end"):
        if key in item and item[key] is not None:
            decided = _age_under_17(item[key])
            if decided is not None:
                return decided

    # Explicit under-17 / child flags.
    for key in ("under_17", "under17", "is_child", "qualifying_child", "qualifying_child_under_17"):
        if key in item and item[key] is not None:
            return _truthy(item[key])

    # A child-ish relationship plus any age<17 signal also qualifies.
    rel = str(item.get("relationship", "")).strip().lower()
    if any(word in rel for word in ("child", "son", "daughter")):
        for key in ("age", "age_years", "age_at_year_end"):
            if key in item and item[key] is not None:
                decided = _age_under_17(item[key])
                if decided:
                    return True

    return False


def _build_dependent(item: dict) -> Dependent:
    """Build a ``Dependent`` from a model-supplied dict, tolerant of extra keys.

    Derives ``is_under_17`` (see :func:`_derive_is_under_17`) and ``has_ssn``
    (True iff a non-empty ``ssn`` is present, unless explicitly overridden), and
    passes ONLY valid ``Dependent`` fields through — unknown keys are dropped.
    Raises ValueError on truly malformed input (e.g. no usable name).
    """
    ssn = item.get("ssn") or ""
    if not isinstance(ssn, str):
        ssn = str(ssn)
    ssn = ssn.strip()

    if "has_ssn" in item and item["has_ssn"] is not None:
        has_ssn = _truthy(item["has_ssn"])
    else:
        has_ssn = bool(ssn)

    fields = {
        "name": item.get("name", ""),
        "ssn": ssn,
        "relationship": item.get("relationship", "") or "",
        "is_under_17": _derive_is_under_17(item),
        "has_ssn": has_ssn,
    }
    # Defensive: never pass keys the model coined (age, etc.) to the model.
    fields = {k: v for k, v in fields.items() if k in _DEPENDENT_FIELDS}
    try:
        return Dependent(**fields)
    except Exception as exc:  # pydantic ValidationError
        raise ValueError(f"invalid dependent {item!r}: {exc}") from exc


def _coerce_dependents(value: Any) -> list[Dependent]:
    """Coerce a model-supplied dependents value into a validated list.

    Accepts: empty / "none" / 0 -> []; a JSON-ish list of dicts -> [Dependent].
    Each dict is interpreted robustly (``_build_dependent``) so the model's
    real-world shapes ({"age": 16}, {"age": "under 17"}, {"qualifying_child":
    true}, ...) correctly set ``is_under_17`` / ``has_ssn`` instead of silently
    defaulting to a $500 Credit for Other Dependents.

    Raises ValueError on anything malformed (caught -> schema guardrail hit).
    """
    if value in (None, "", 0, "0", "none", "None", "no", "No", False, []):
        return []
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"dependents must be a list, got {value!r}") from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"dependents must be a list, got {type(value).__name__}")
    deps: list[Dependent] = []
    for item in value:
        if isinstance(item, Dependent):
            deps.append(item)
        elif isinstance(item, dict):
            deps.append(_build_dependent(item))
        elif isinstance(item, str):
            deps.append(Dependent(name=item))
        else:
            raise ValueError(f"invalid dependent entry: {item!r}")
    return deps


def _facts_from_session(session) -> TaxFacts:
    """Assemble validated ``TaxFacts`` from the session's filled slots."""
    w2 = session.slots.w2
    if w2 is None:
        raise ValueError("cannot compute without a W-2")
    if session.slots.filing_status not in VALID_FILING_STATUSES:
        raise ValueError("cannot compute without a valid filing status")
    return TaxFacts(
        filing_status=session.slots.filing_status,
        wages=w2.wages,
        fed_withholding=w2.fed_withholding,
        dependents=list(session.slots.dependents),
    )


# ---------------------------------------------------------------------------
# The single dispatch path (P2). The ONLY mutator of slots / result / pdf.
# ---------------------------------------------------------------------------
def dispatch(tool_call: ToolCall, session) -> dict:
    """Execute one tool call against the session. THE ONLY state-mutating path.

    Returns a result dict. Also sets ``tool_call.result`` / ``tool_call.ok`` and,
    on a schema failure, stores a ``GuardrailHit`` on ``tool_call._guardrail`` so
    the orchestrator can drain it into the turn Event (R3.1). ``slot_changes``
    are returned under the ``"slot_changes"`` key.

    Schema / validation failures are caught here and turned into a structured
    ``{ok: False, error, guardrail}`` result rather than raising — so one bad
    tool call never crashes the turn.
    """
    name = tool_call.name
    args = dict(tool_call.args or {})
    args.pop("_tool_use_id", None)
    slot_changes: dict = {}

    def _fail(rule: str, detail: str, slot: str = "") -> dict:
        hit = GuardrailHit(rule=rule, detail=detail, slot=slot)
        tool_call.ok = False
        tool_call.result = {"ok": False, "error": detail}
        # Stash so the orchestrator can attach it to the Event.
        setattr(tool_call, "_guardrail", hit)
        return {"ok": False, "error": detail, "guardrail": hit, "slot_changes": {}}

    try:
        if name == "parse_w2":
            from app.w2 import parse_w2  # validated extraction; raises ValueError

            try:
                w2: W2 = parse_w2(
                    raw_text=args.get("raw_text"),
                    image_b64=args.get("image_b64"),
                )
            except ValueError as exc:
                # _build_w2 raises a complete, user-facing sentence naming the
                # exact problem (missing Box 1 wages, unparseable Box 2, missing
                # name/SSN, or "not a W-2"). Pass it through verbatim so the model
                # can relay that specific question and the user can re-paste a fix.
                return _fail("schema", str(exc), slot="w2")
            session.slots.w2 = w2
            slot_changes["w2"] = {
                "employee_name": w2.employee_name,
                "ssn": w2.ssn,  # redacted on display by trace.py
                "wages": w2.wages,
                "fed_withholding": w2.fed_withholding,
            }
            out = {
                "ok": True,
                "wages": w2.wages,
                "fed_withholding": w2.fed_withholding,
                "employee_name": w2.employee_name,
            }
            tool_call.ok = True
            tool_call.result = out
            return {**out, "slot_changes": slot_changes}

        if name == "set_slot":
            slot = args.get("name")
            value = args.get("value")
            if slot == "filing_status":
                fs = str(value).strip().lower()
                # Light normalization of common phrasings.
                fs = {
                    "married filing jointly": "mfj",
                    "married filing separately": "mfs",
                    "head of household": "hoh",
                    "qualifying surviving spouse": "qss",
                    "qualifying widow": "qss",
                    "married": "mfj",
                }.get(fs, fs)
                if fs not in VALID_FILING_STATUSES:
                    return _fail(
                        "schema",
                        f"unknown filing status {value!r}; must be one of "
                        f"single, mfj, mfs, hoh, qss",
                        slot="filing_status",
                    )
                session.slots.filing_status = fs
                slot_changes["filing_status"] = fs
                out = {"ok": True, "normalized": fs}
            elif slot == "dependents":
                try:
                    deps = _coerce_dependents(value)
                except ValueError as exc:
                    return _fail("schema", str(exc), slot="dependents")
                session.slots.dependents = deps
                session.slots.dependents_answered = True
                slot_changes["dependents"] = [d.name for d in deps]
                out = {"ok": True, "count": len(deps)}
            elif slot == "confirm":
                truthy = value in (True, "true", "True", "yes", "Yes", "y", 1, "1")
                session.slots.confirmed = bool(truthy)
                slot_changes["confirmed"] = bool(truthy)
                out = {"ok": True, "confirmed": bool(truthy)}
            else:
                return _fail("schema", f"unknown slot {slot!r}", slot=str(slot or ""))
            tool_call.ok = True
            tool_call.result = out
            return {**out, "slot_changes": slot_changes}

        if name == "compute_1040":
            try:
                facts = _facts_from_session(session)
            except ValueError as exc:
                return _fail("schema", str(exc))
            from app.compute import compute_1040  # LAZY (agent A) — frozen sig

            result: Form1040Result = compute_1040(facts)
            session.result = result
            slot_changes["result"] = {
                "line_15": result.line_15,
                "line_16": result.line_16,
                "refund": result.refund,
                "owed": result.owed,
            }
            out = {
                "ok": True,
                "line_15_taxable": result.line_15,
                "line_16_tax": result.line_16,
                "refund": result.refund,
                "owed": result.owed,
            }
            tool_call.ok = True
            tool_call.result = out
            return {**out, "slot_changes": slot_changes}

        if name == "fill_1040_pdf":
            if session.result is None:
                return _fail("schema", "cannot fill the PDF before compute_1040 succeeds")
            path = _ensure_pdf(session)
            out = {"ok": True, "path": path}
            tool_call.ok = True
            tool_call.result = out
            return {**out, "slot_changes": slot_changes}

        if name == "correct_slot":
            # Stretch: overwrite a prior answer WITHOUT counting a question.
            slot = args.get("name")
            value = args.get("value")
            if slot == "filing_status":
                fs = str(value).strip().lower()
                if fs not in VALID_FILING_STATUSES:
                    return _fail("schema", f"unknown filing status {value!r}", slot="filing_status")
                old = session.slots.filing_status
                session.slots.filing_status = fs
                slot_changes["filing_status"] = {"old": old, "new": fs}
            elif slot == "dependents":
                try:
                    deps = _coerce_dependents(value)
                except ValueError as exc:
                    return _fail("schema", str(exc), slot="dependents")
                old = [d.name for d in session.slots.dependents]
                session.slots.dependents = deps
                session.slots.dependents_answered = True
                slot_changes["dependents"] = {"old": old, "new": [d.name for d in deps]}
            else:
                return _fail("schema", f"cannot correct slot {slot!r}", slot=str(slot or ""))
            # A correction invalidates any downstream computed artifacts.
            session.result = None
            session.pdf_path = None
            session.slots.confirmed = False
            out = {"ok": True, "corrected": slot}
            tool_call.ok = True
            tool_call.result = out
            return {**out, "slot_changes": slot_changes}

        # Unknown tool name.
        return _fail("schema", f"unknown tool {name!r}")

    except Exception as exc:  # never let a tool crash the whole turn
        return _fail("schema", f"tool {name} failed: {exc}")


def _ensure_pdf(session) -> str:
    """Fill the IRS 1040 PDF for this session if not already done; return path.

    Lazily imports ``app.pdf_fill`` (agent B). Idempotent: reuses an existing
    ``session.pdf_path`` if the file is still on disk.
    """
    if session.pdf_path and os.path.exists(session.pdf_path):
        return session.pdf_path
    if session.result is None:
        raise ValueError("cannot fill the PDF before compute_1040 succeeds")

    from app.pdf_fill import fill_1040_pdf  # LAZY (agent B) — frozen sig

    w2 = session.slots.w2
    identity = Identity.from_w2(w2) if w2 is not None else Identity(
        first_name="", last_name="", ssn=""
    )
    out_dir = tempfile.gettempdir()
    out_path = os.path.join(out_dir, f"f1040_{session.id}.pdf")
    written = fill_1040_pdf(
        result=session.result,
        identity=identity,
        filing_status=session.slots.filing_status or "single",
        dependents=list(session.slots.dependents),
        out_path=out_path,
        flatten=True,
    )
    session.pdf_path = written or out_path
    return session.pdf_path


__all__ = ["TOOLS", "TOOL_NAMES", "dispatch"]
