# PRD — Agentic Tax-Filing Assistant (Form 1040, TY2025)

Status: **build-ready**. Companion docs: [plan.md](./plan.md) (strategy),
[architecture.md](./architecture.md) (how to build it).
Repo root: `C:\Users\kenhu\gauntlet\hackathon`.

---

## 1. Goal
A web-based chat where a person with a single ~$40k W-2 has a short, warm
conversation (≤ 5 questions) and walks away with a **downloadable, correctly
filled 2025 IRS Form 1040**. The system is deployed to a public URL a judge can
reach. We are judged, in priority order, on: **(1) harness quality** — how real
and enforced the four pillars are; **(2) does it actually work** end-to-end;
**(3) conversation quality**; **(4) soundness of our decisions.**

The thesis that wins (1) and (2) at once: **a deterministic Python tax core the
LLM is structurally forbidden from doing math in.** The LLM only *talks* and
*extracts*; code computes the return and fills the real IRS PDF.

---

## 2. The four pillars — as testable requirements
A judge must point at our **code** and our **running system** and see each one.
"It's in the prompt" fails the bar; each pillar below is an enforced code
invariant **plus** a visible runtime surface.

### P1 — Chat loop (carries state across turns)
- **R1.1** Server holds per-session state `{session_id → slots, transcript, event_log, questions_asked}` as a typed object.
- **R1.2** Each `POST /chat` turn advances an explicit **state machine**; the machine (not the model) decides the next action.
- **R1.3 [enforced]** State survives across turns in the running system — **deploy pinned to one instance** so in-memory state is never lost mid-conversation (see §6).
- **Visible:** `GET /trace/{session}` shows the typed state mutating turn over turn.

### P2 — Tools (real actions, not talk; must produce the filled return)
- **R2.1** The agent acts **only** through typed tools: `parse_w2`, `set_slot`, `compute_1040`, `fill_1040_pdf` (+ stretch `correct_slot`).
- **R2.2 [enforced — BLOCKER if missed]** A single `dispatch(tool_call)` is the **only** code path that mutates state or triggers compute. The orchestrator reads **only** `tool_use` blocks from the model and **never** scrapes numbers from prose `text`. 
- **R2.3** `fill_1040_pdf` produces the official IRS AcroForm PDF — the required "something that produces the filled return."
- **Test (proof):** a turn where the model puts "wages are $40,000" in prose with **no tool call** leaves all slots empty and fires no compute.
- **Visible:** every tool call (name, args, return) appears in the trace.

### P3 — Guardrails (on-task, safe, bounded — in code, not prompt)
- **R3.1 Schema:** Pydantic models reject malformed / out-of-range W-2 and slot values; a rejected parse is a visible event.
- **R3.2 Scope [enforced — not prompt-only]:** off-topic / "give me tax advice" / non-1040 input is caught by a **code gate before the model is asked for content**, emits a deterministic refusal + fixed disclaimer ("educational tool, not tax advice"), and **does not consume a question**.
- **R3.3 Budget [enforced — BLOCKER if missed]:** a hard counter caps **information questions at 5** (counting rule in §4). `compute_1040` runs only when all required slots are valid; the **download link unlocks only after compute succeeds**.
- **R3.4 PII:** SSN is redacted to `XXX-XX-NNNN` everywhere except the final PDF; the trace never shows full SSN.
- **Visible:** each guardrail hit is a red row in the trace `{type:"guardrail_hit", rule, slot}`.

### P4 — Observation (see what it did, decided, and acted)
- **R4.1** Every turn appends a structured event: `{turn, user_msg, tool_calls[], slot_changes, guardrail_hits, decision, model_latency_ms}`.
- **R4.2 [decision visibility]** `decision = {state_before, state_after, next_action, reason}` — e.g. `reason:"slot 'dependents' empty; budget 2/5"`. This makes the harness's *reasoning* legible, not just its actions.
- **R4.3** Exposed two ways: `GET /trace/{session}` (JSON) **and** a collapsible trace panel in the chat UI (**core, not stretch** — the rubric requires seeing pillars in the running system).
- **Reuse caveat:** keep two surfaces — a tamper-evident hashed audit event (NovaTax `auditTrailService` pattern) **and** a separate plaintext-but-PII-redacted *display* event. Do not hash the thing you need to display.

---

## 3. Tax requirements (correctness is the product)
Constants are reused **verbatim** from NovaTax's tested
`C:\Users\kenhu\novatax\services\taxConstants\2025.ts` (post-OBBBA, IRS Rev.
Proc. 2024-40 + H.R.1/2025) — **diff-verified exact** during review. Import a
frozen copy; do not retype.

- **R-TAX.1 Standard deduction (TY2025):** Single $15,750 · MFJ $31,500 · MFS $15,750 · HoH $23,625 · QSS $31,500.
- **R-TAX.2 Brackets:** 2025 tables for all five statuses (Single 10%≤$11,925 · 12%≤$48,475 · 22%≤$103,350 · 24%≤$197,300 · 32%≤$250,525 · 35%≤$626,350 · 37%; others in architecture.md — note HoH 32% top is $250,**500**, not $250,525).
- **R-TAX.3 Line 16 = IRS Tax Table, not the raw bracket formula** (required for taxable income < $100k). Tax on the **midpoint of the $50 bracket**: `mid = (taxable//50)*50 + 25`, then bracket formula on `mid`, rounded to whole dollars (half-up). For $40k single: taxable $24,250 → **$2,675** (the raw formula gives $2,672 — a visible $3 error against the official table). Valid for taxable ≥ $3,000 (always true here).
- **R-TAX.4 All money lines rounded to whole dollars**, half-up.
- **R-TAX.5 Refund vs owe are mutually exclusive:** if payments > total tax → Line 34/35a refund, Line 37 = 0; else Line 37 owe, Line 34 = 0.

### Scope of the tax engine
**CORE (must work, and is fully correct at ~$40k):**
- Filing status **Single / MFJ / MFS**, one W-2, **no dependents**.
- At $40k childless, **CTC = $0 and EITC = $0** (childless EITC fully phases out at $19,104 single) — so the core needs only: wages → AGI → std deduction → taxable → tax-table → compare withholding. Clean and correct with zero credit logic.

**STRETCH (graceful, gated, documented):**
- **Dependents** → Child Tax Credit $2,200/qualifying child (under 17 + SSN) vs **Credit for Other Dependents $500** (split by age/SSN), nonrefundable-capped to tax; **Additional CTC** (refundable, Sch 8812) for the multi-child case; **EITC** for the with-children case (material at $40k — ~$1,667 for one child — implement it or guardrail+document, never silently drop it).
- **HoH / QSS** are **gated on having a qualifying dependent** (a HoH/QSS claim with zero dependents is invalid → guardrail refuses/repairs). So they belong to the dependents stretch.
- **MFS** assumes the spouse also takes the standard deduction (document); MFS → EITC $0.
- **Mid-conversation correction** (change a prior answer without burning a question).

**OUT OF SCOPE (assert/zero + disclaimer):** itemized deductions, age/blind, adjustments to income, any income beyond one W-2, state tax, e-filing, real PII/real filing.

---

## 4. Conversation design (warm, human, ≤ 5 questions)
The W-2 supplies wages, withholding, **and identity** (name, SSN, address — the
fixture must include them), so questions are only for what the W-2 can't tell us.

**Question-counting rule [enforced]:** a "question" is counted **once, on entry
to an `AWAIT_*` state that solicits new information for an unfilled required
slot.** Do **not** count: the final confirmation, value-confirmation/repair
prompts, guardrail refusals, same-slot re-prompts, or internal LLM retries.
Repairs are bounded by a **separate** `max_repairs_per_slot = 2`. The gate is a
precondition: `if next_action==ASK and questions_asked>=5: force COMPUTE`.

**Happy path (clean W-2, single, no dependents): 2 info questions + 1 confirm → 2 of 5 used.**

| Turn | State | Assistant (tone: acknowledge, then one question) | Counts? |
|---|---|---|---|
| 1 | AWAIT_W2 → AWAIT_FILING_STATUS | "Got it — W-2 loaded, $40,000 in wages. First, how are you filing this year — single, married filing jointly, or head of household?" | Q1 |
| 2 | AWAIT_DEPENDENTS | "Thanks! Anyone you support — kids or dependents — I should count?" | Q2 |
| 3 | CONFIRM_FACTS | "Here's what I have: single, $40,000 wages, $X withheld, no dependents. Want me to go ahead and fill your 1040?" | confirm (not counted) |

**Tone rules (system prompt — tone only; flow/correctness live in code):** one
question per turn; acknowledge what just happened first; plain language, options
inline as examples; mirror the user's words on confirm; one sentence of *why* for
sensitive asks ("so I can check the Child Tax Credit"); **the model may never
volunteer numbers or tax conclusions — only restate tool-computed values.**

---

## 5. End-to-end acceptance criteria (the "does it actually work" gate)
Run by a **browser agent against the deployed public URL** (FN-009). All must pass:
1. Page loads (< ~15s cold) and renders the chat UI; **zero console errors**.
2. Load sample ~$40k W-2 → answer filing status → dependents → confirm; **chat never resets to Q1** between turns (proves session pinning).
3. Off-topic / "give me tax advice" → fixed educational refusal; **does not consume a slot**; agent never exceeds 5 questions.
4. Download is **locked until `compute_1040` succeeds**, then unlocks.
5. `GET /download/{session}` → HTTP 200, `Content-Type: application/pdf`, non-trivial size, **AND the AcroForm fields contain the expected values** (L1a ≈ Box 1, L25a ≈ Box 2, L16 = golden tax, L34/L37 = golden refund/owe). A blank-but-downloading PDF is the classic false-green — check field values.
6. PDF renders **with visible values in Chrome's built-in viewer** (see architecture §PDF — flattened copy guarantees this).
7. `GET /trace/{session}` returns structured per-turn events and the UI trace panel is present.

---

## 6. Deliverables (mapped to the spec)
- **D1 — Source in a repository [GAP: currently local-only].** Push to GitHub (public): `gh repo create tax1040-assistant --public --source . --remote origin --push`. Verify `.env*` is gitignored and no key is tracked first.
- **D2 — Live public URL** on Google Cloud Run (comparable free/easy host; chosen over Render — justify in DECISIONS). Publicly reachable, no auth.
- **D3 — One-command local run** (fallback, not a substitute): both `docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... tax1040` and `uvicorn app.main:app` documented in README.
- **D4 — DECISIONS.md (~½ page)** defending: Cloud Run over Render; Python+FastAPI single process; `claude-opus-4-8` (rejects `temperature`/prefill); deterministic core; official AcroForm fill vs render-from-scratch; in-memory sessions + instance pin (trade-off stated); Tax-Table-vs-formula; dependents/EITC scope call.
- **D5 — Sample fake W-2** (~$40k, full identity) + a one-click "load sample" button so a judge can try it without their own W-2.

---

## 7. Non-negotiable constraints (from the brief)
Form 1040 TY2025 · taxpayer ~$40k single W-2 · filing-status changes supported ·
≤ 5 questions · genuinely friendly tone · downloadable completed form · web chat ·
publicly deployed · works end-to-end (not a happy-path mock) · fake data only, no
real PII / no e-filing · not tax advice (and the agent must not pretend to give it).

## 8. Top risks (and the mitigation that retires each)
| Risk | Mitigation | Owner task |
|---|---|---|
| Filled PDF renders **blank** in a browser | pypdf writes appearance streams; ship a **flattened** copy; drop `/XFA` | FN-002 |
| LLM prose becomes a write path (P2 cosmetic) | single `dispatch()`; prose-only-no-tool test | FN-003/005 |
| 5-question budget blown on unhappy path | counting rule + separate repair budget | FN-003/004 |
| Session resets mid-demo on Cloud Run | `--min-instances=1 --max-instances=1` | FN-007 |
| First LLM call 500s after deploy | grant runtime SA `secretmanager.secretAccessor` | FN-007 |
| Required deliverable missing | push repo to GitHub | new task |
| Line 16 off by $3 vs IRS | Tax-Table midpoint rule | FN-001 |
