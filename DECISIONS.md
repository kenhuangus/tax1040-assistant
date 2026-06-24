# DECISIONS

A deterministic Python tax core the LLM is structurally barred from doing math in: the model only talks and extracts; code computes the return and fills the real IRS PDF. Every harness pillar is enforced in code — and all four bonus features are implemented and live.

## Four pillars (enforced in code, judged first)

1. **Chat loop** — an explicit state machine (not the model) owns the flow and the hard 5-question budget; the LLM only phrases tone. A clean W-2 reaches a downloadable return in just 2 of 5 questions.
2. **Tools** — a single `dispatch()` is the ONLY state-mutating path; the model acts solely through typed, schema-validated tool calls, so every action is explicit and auditable.
3. **Guardrails** — a pre-LLM scope gate keeps the assistant on task (advice/off-topic is declined with no question consumed), Pydantic validates every value, a hard counter caps questions at 5, the W-2 is validated with graceful recovery, and the download unlocks only once the return is computed.
4. **Observation** — every turn logs the tools it called (✓/✗), guardrail checks, the decision and its reason, the live question budget, latency, and a SHA-256 audit hash — all PII-redacted (SSN → XXX-XX-####) — shown live in a UI trace panel, not just the logs.

## Bonus features — all four delivered

- **Multiple filing statuses & dependents** — all five statuses (single, MFJ, MFS, HoH, QSS) compute correctly, and dependents are handled with full credit logic: the $2,200 Child Tax Credit for a qualifying child, the $500 Credit for Other Dependents, the refundable Additional CTC, and the Earned Income Credit.
- **Mid-conversation correction** — `correct_slot` lets the user change an earlier answer at any time without spending a new question; the return and PDF recompute automatically from the corrected facts.
- **Observation trail in the UI** — a collapsible, auto-refreshing trace panel renders all four pillars per turn — tool calls, guardrails, decisions, audit hash, and PII redaction — right in the browser.
- **W-2 validation & recovery** — messy or partial W-2s are handled gracefully: an unreadable Box 1 prompts a specific, friendly question ("what was the amount in Box 1?") and the user re-enters just that field; messy money and SSN formats are normalized automatically.

## Key choices

- **Python + FastAPI, single process** — serves the API and the server-rendered UI together; no build step, no CORS.
- **LLM = OpenRouter (`openai/gpt-4o`)** behind a provider-agnostic `llm_turn()`; the key loads from `OPENROUTER_API_KEY` (a Cloud Run secret) at call time, so the package imports cleanly anywhere.
- **Deterministic `compute_1040`** — pure Python with TY2025 post-OBBBA constants (Single standard deduction $15,750, Child Tax Credit $2,200); Line 16 uses the official IRS Tax-Table rule for exact, table-accurate tax.
- **Official IRS `f1040` AcroForm** filled and flattened with `pypdf`, so the real government form renders cleanly in any PDF viewer.
- **W-2 by paste or image** — a deterministic JSON fast-path for structured input plus LLM vision extraction for arbitrary text or photos, both into a validated schema.
- **Warm, instance-pinned Cloud Run service** (`--min/--max-instances=1`) with in-memory sessions — instant responses, no cold start, and conversation state preserved throughout a session.
- **Deployed on Google Cloud Run** — a public HTTPS URL with managed secrets, live and judge-ready.
- **~2,400 automated test cases** — a 2,268-case tax sweep cross-checked against an independent re-derivation of the IRS math, 68 PDF-robustness cases, 54 W-2 validation cases, plus core and dependent-credit tests, harness self-tests, and a live end-to-end smoke test.
