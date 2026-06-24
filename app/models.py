"""
Shared Pydantic models — the FROZEN cross-module contract.

EVERY module imports its types from here. Do NOT redefine these elsewhere.
Owned by the lead orchestrator; agents must not edit this file (if a field is
missing for your module, note it in your summary — do not silently change it).

Money is always whole-dollar ints. SSN is stored raw only inside W2/Identity for
the final PDF; everything user-facing/logged must redact via guardrails.redact_ssn.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field

FilingStatus = Literal["single", "mfj", "mfs", "hoh", "qss"]


# ---------- inputs ----------
class Dependent(BaseModel):
    name: str
    ssn: str = ""
    relationship: str = ""
    is_under_17: bool = False
    has_ssn: bool = True


class W2(BaseModel):
    """Extracted W-2. Box 1 = wages, Box 2 = federal income tax withheld."""
    employee_name: str
    ssn: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    employer: str = ""
    wages: int                  # Box 1
    fed_withholding: int        # Box 2


class Identity(BaseModel):
    first_name: str
    last_name: str
    ssn: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    @classmethod
    def from_w2(cls, w2: "W2") -> "Identity":
        first, _, last = w2.employee_name.partition(" ")
        return cls(first_name=first, last_name=last or first, ssn=w2.ssn,
                   address=w2.address, city=w2.city, state=w2.state, zip=w2.zip)


class TaxFacts(BaseModel):
    filing_status: FilingStatus
    wages: int
    fed_withholding: int
    dependents: list[Dependent] = Field(default_factory=list)


# ---------- compute output ----------
class Form1040Result(BaseModel):
    """Every populated 1040 line, whole dollars. line_34/owed are mutually exclusive."""
    line_1a: int = 0
    line_1z: int = 0
    line_9: int = 0
    line_11: int = 0
    line_12: int = 0
    line_13: int = 0
    line_14: int = 0
    line_15: int = 0
    line_16: int = 0
    line_17: int = 0
    line_18: int = 0
    line_19: int = 0
    line_20: int = 0
    line_21: int = 0
    line_22: int = 0
    line_23: int = 0
    line_24: int = 0
    line_25a: int = 0
    line_25d: int = 0
    line_27: int = 0
    line_28: int = 0
    line_32: int = 0
    line_33: int = 0
    line_34: int = 0     # refund (overpayment)
    line_35a: int = 0    # refund applied to you
    line_37: int = 0     # amount you owe
    refund: int = 0      # convenience: == line_34
    owed: int = 0        # convenience: == line_37


# ---------- observation (P4) ----------
class ToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    result: Optional[dict] = None
    ok: bool = True


class GuardrailHit(BaseModel):
    rule: str                # "scope" | "schema" | "budget" | "domain"
    detail: str = ""
    slot: str = ""


class Decision(BaseModel):
    state_before: str
    state_after: str
    next_action: str         # ASK | CONFIRM | COMPUTE | REPAIR | REFUSE
    reason: str


class Event(BaseModel):
    turn: int
    user_msg: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    slot_changes: dict = Field(default_factory=dict)
    guardrail_hits: list[GuardrailHit] = Field(default_factory=list)
    decision: Optional[Decision] = None
    latency_ms: int = 0


# ---------- guardrail / llm helpers ----------
class ScopeResult(BaseModel):
    ok: bool
    refusal: Optional[str] = None
    rule: str = "scope"
