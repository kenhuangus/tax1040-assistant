# Winning Plan — Agentic Tax-Filing Assistant (Form 1040, TY2025)

## The one insight that drives everything
Judging weight is **harness quality first, then "does it actually work."** The
sharpest way to win both at once: **a deterministic tax core that the LLM is
structurally forbidden from doing math in.** The LLM only *talks* and *extracts*;
a pure-Python module computes the return and fills the real IRS PDF. That makes
all four pillars *enforced and visible*, not "it's in the prompt."

> Recurring winning pattern: deterministic core + LLM as a narrow, gated
> participant. Tax is arithmetic with rules — never let the model do the math.

## Architecture (one process, clean seams)

```
Browser chat ──HTTP──> FastAPI
                         ├─ /chat   -> Orchestrator (the harness)
                         │             ├─ Chat loop      (state machine + LLM for wording)
                         │             ├─ Tools          (typed, the only way to act)
                         │             ├─ Guardrails     (Pydantic + rule checks, code-enforced)
                         │             └─ Observation    (structured event log per session)
                         └─ /download/{session} -> filled f1040.pdf
```

### The harness = an explicit state machine, not a free-roaming agent
A finite set of slots to fill: `filing_status`, `w2_wages`, `fed_withholding`,
`dependents`, `confirm`. The LLM's job each turn is (a) read the user's message,
(b) call a tool to record/extract a slot, (c) phrase the next question warmly.
The state machine — not the model — decides what's still needed and when to file.
This is what makes the 5-question budget *guaranteed* rather than hoped-for.

## The Four Pillars — how each is REAL and VISIBLE (judges look here)

**Code anchors (so a judge can literally point at each pillar):**
- Chat loop → `orchestrator.py::advance()` (+ `POST /chat`) and `session.py` (typed state)
- Tools → `tools/registry.py` (the 4 typed tools; `fill_1040_pdf` produces the return)
- Guardrails → `guardrails.py` (schema + scope + budget, all code-enforced)
- Observation → `trace.py` + `GET /trace/{session}` + the live trace UI panel

1. **Chat loop** — Server-held session state (`session_id` -> slots + transcript +
   event log). Each turn advances the state machine. State carried across turns is
   literally a typed object you can print.
2. **Tools** — The agent acts ONLY through typed tools, never free text:
   - `parse_w2(raw)` -> structured W-2 (Pydantic-validated)
   - `set_slot(name, value)` -> validated write into state
   - `compute_1040(facts)` -> deterministic tax result
   - `fill_1040_pdf(result)` -> writes the official IRS PDF, returns download path
   Expose the tool registry + each call's args/returns in the observation trail.
3. **Guardrails** — enforced in *code*, three layers:
   - **Schema**: Pydantic models reject malformed W-2 / out-of-range values.
   - **Scope**: refuses off-topic / "give me tax advice" / non-1040 asks with a
     fixed disclaimer ("educational, not tax advice").
   - **Budget**: hard counter — max 5 questions; computation only runs once all
     required slots are valid; download only unlocks after `compute_1040` succeeds.
4. **Observation** — every turn appends a structured event
   `{turn, user_msg, tool_calls[], slot_changes, guardrail_hits, model_latency}`.
   Exposed two ways: `/trace/{session}` JSON endpoint **and** a collapsible panel
   in the chat UI. **The UI panel is CORE, not stretch** — the rubric says a judge
   must see each pillar in the *running system*, not just the code; the live trace
   panel is the cheapest way to make all four pillars visibly legible. (FN-005.)

## The tax computation (deterministic, TY2025, defensible)
Single W-2, ~$40k. Compute in pure Python. **Constants are reused verbatim from
NovaTax's tested/proven `services/taxConstants/2025.ts` (post-OBBBA, IRS Rev.
Proc. 2024-40 + H.R.1/2025), so the figures are already audited:**
- AGI = Box 1 wages (no adjustments for this profile).
- **Standard deduction TY2025 (post-OBBBA): Single $15,750 / MFJ $31,500 /
  MFS $15,750 / HoH $23,625 / QSS $31,500.**
- **2025 brackets (single):** 10% ≤ $11,925 · 12% ≤ $48,475 · 22% ≤ $103,350 ·
  24% ≤ $197,300 · 32% ≤ $250,525 · 35% ≤ $626,350 · 37% above.
  (MFJ/HoH/MFS/QSS tables transcribed from the same file.)
- **Child Tax Credit: $2,200/child** (OBBBA raised §24 from $2,000), phaseout
  $200k single / $400k MFJ.
- Taxable income = max(0, AGI − std deduction).
- Tax via the bracket table for the filing status.
- Compare tax vs. Box 2 withholding -> refund or amount owed.
- Map every number to the correct 1040 line, then to the PDF field name.

> Reuse policy: where NovaTax has tested/proven code we can lift, we do — the
> 2025 constants, the audit-trail/observation pattern (`auditTrailService.ts` +
> `auditTrailRender.ts`), the validation approach (`globalValidationService.ts`),
> the W-2 schema (`schemas.ts`), and PII redaction (`piiRedaction.ts`,
> SSN→`XXX-XX-NNNN`). We do NOT reuse `pdfGenerator.ts` — it renders a 1040-like
> summary from scratch; the hackathon wants the OFFICIAL IRS AcroForm filled, so
> we keep our `pypdf` fill of the real `f1040.pdf` (229 fields, verified).

## Filling the real 1040  ⚠️ HIGHEST-RISK STEP — DO THIS FIRST
- Ship the official `assets/irs/f1040--2025.pdf` (already downloaded; 229 AcroForm
  fields, confirmed fillable, 2 pages).
- **Reality check from inspecting the PDF: 0 of 229 fields have human labels or
  tooltips (`/TU`).** Names are fully opaque (`f1_01[0]`, `c1_3[0]`…), so there is
  NO built-in way to know which field is "Line 1a wages" vs "Line 16 tax." This is
  the most underestimated task — de-risk it before the LLM layer.
- **Approach:** every field DOES carry a widget rectangle. Render each page to PNG,
  stamp each field id at its rect position, eyeball the ~12–15 fields we actually
  need, and hardcode a frozen `LINE -> field_id` map with a comment per line.
- We need only ~12–15 of 229: name, SSN (redacted), filing-status checkbox,
  dependents, **L1a** wages, **L11** AGI, **L12** std ded, **L15** taxable,
  **L16** tax, **L19** CTC, **L22**, **L24** total tax, **L25a** withholding,
  **L33/34** refund, **L37** amount owed. Map only those.
- Fill with `pypdf` and set `NeedAppearances` so values render; return as download.

## The 5 questions (warm, minimal, in order)
W-2 upload/paste covers wages + withholding, so questions are only for what the
W-2 can't tell us:
1. "First, how are you filing this year — single, married filing jointly, or head
   of household?"  (filing_status)
2. "Anyone you support — kids or dependents — I should count?" (dependents)
3. (If the W-2 paste is ambiguous) "I read $X in wages and $Y withheld — does that
   match your W-2?" (confirm/repair extraction)
4. reserved — only asked if a slot is missing/invalid
5. reserved — final "Want me to go ahead and fill your 1040?" confirm
Tone rules live in the system prompt; *correctness and flow* live in code.

## Stack & deployment (chosen for speed + reliability)
- **Python + FastAPI**, single service, server-rendered minimal HTML/JS chat (no
  build step — front-end polish is explicitly NOT judged).
- **LLM: Anthropic `claude-opus-4-8`** (key already in env) for conversation +
  W-2 extraction, with tool use. Note: this model rejects `temperature` and
  assistant-prefill — don't send them.
- **W-2 input**: paste text OR upload an image; LLM extracts to the Pydantic model.
  Ship a realistic fake W-2 (~$40k) as test fixture + a "load sample" button.
- **Deploy: Google Cloud Run** (NOT Render). Single Dockerfile, read `PORT` from
  env, bind `0.0.0.0`, public/unauthenticated so a judge can reach it.
  `ANTHROPIC_API_KEY` via Secret Manager (grant the runtime SA
  `roles/secretmanager.secretAccessor` — not automatic). Health endpoint
  `/api/healthz` (GFE reserves `/healthz`). Stateless except in-memory sessions
  (fine for a prototype; note the trade-off in DECISIONS).

## How we prove it works (testing)
- Unit: `compute_1040` golden cases (single $40k, MFJ, HoH) with hand-checked numbers.
- Unit: guardrails (off-topic refusal, >5 questions blocked, malformed W-2 rejected).
- Integration: scripted full conversation -> asserts a non-empty filled PDF whose
  fields contain the expected line values.
- Browser-agent live QA gate against the deployed Cloud Run URL before calling it done.

## Deliverables checklist
- [ ] Repo (this dir) with source + smoke tests
- [ ] Live Google Cloud Run URL a judge can try
- [ ] One-command local run (`docker run` or `uvicorn`) in README
- [ ] `DECISIONS.md` (~half page): the open-item choices + why

## Build order (working end-to-end fast, then hardened)
0. **De-risk first:** PDF field map (render+overlay, freeze ~15 fields) — the
   opaque-field-name problem is the #1 risk; prove a hand-filled 1040 downloads.
1. `compute_1040` + golden tests (the defensible core).
2. `fill_1040_pdf` wired to the frozen field map.
3. FastAPI + state machine + tools, wired to the above (happy path E2E).
4. LLM conversation layer + W-2 extraction.
5. Guardrails + observation trail **incl. the live trace UI panel** (harness showcase).
6. Deploy to Google Cloud Run + browser-agent live QA gate.
7. DECISIONS.md + sample W-2 fixture.
