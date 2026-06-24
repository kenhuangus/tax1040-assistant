# DECISIONS

A deterministic Python tax core the LLM is structurally barred from doing math in: the model only talks and extracts; code computes the return and fills the real IRS PDF.

## Four pillars (enforced in code, judged first)

1. **Chat loop** — an explicit state machine (not the model) owns flow and the hard 5-question budget; the LLM only phrases tone. Happy path: 2 of 5 questions.
2. **Tools** — a single `dispatch()` is the ONLY state-mutating path; the model acts solely through typed, schema-validated tool calls; any number it writes in prose is ignored.
3. **Guardrails** — a pre-LLM scope gate refuses advice/off-topic (no question consumed), Pydantic validates every value, a hard counter caps questions at 5, the W-2 is validated with graceful recovery (an unreadable Box 1 is asked for, not zeroed), and download stays locked until compute succeeds.
4. **Observation** — every turn logs tools (✓/✕), guardrail hits, decision + reason, budget, latency, and a SHA-256 audit hash, PII-redacted (SSN → XXX-XX-####), shown in a UI trace panel — not just the logs.

## Key choices

- **Python + FastAPI, single process** — serves API and server-rendered HTML; no build step, no CORS.
- **LLM = OpenRouter (`openai/gpt-4o`)** behind a provider-agnostic `llm_turn()`; key from `OPENROUTER_API_KEY` (Cloud Run secret), so the package imports with no key set.
- **Deterministic `compute_1040`** — pure Python, TY2025 post-OBBBA constants (Single std deduction $15,750, CTC $2,200); Line 16 uses the IRS Tax-Table midpoint rule, not the raw bracket formula (off by $3 at $40k).
- **Official IRS `f1040` AcroForm** filled and flattened with `pypdf` (flatten so values render in Chrome's viewer, not blank).
- **W-2 via paste or image** — bundled sample takes a deterministic JSON fast-path; arbitrary text/images go through LLM vision into a Pydantic schema.
- **In-memory sessions pinned to one Cloud Run instance** (`--min/--max-instances=1`) — no state loss, no cold start; no horizontal scale (fine for a single judge).
- **Hosted on Google Cloud Run** — `gcloud` already installed and authed; lowest-friction public URL with secrets + instance pinning.
- **~2,400 automated test cases** — 2,268-case tax sweep + 68 PDF robustness + 54 W-2 validation + core/dependent tests; plus harness self-tests and `scripts/smoke_e2e.py` live smoke.

**Scope:** core = single filer with one W-2, fully correct at ~$40k. All 5 statuses compute correctly, but MFJ/MFS spouse SSN/name is left for the filer (protects the 5-Q budget). Dependents / CTC / EITC / HoH / QSS are a guarded stretch. Itemized, state, e-file, and real PII are out of scope — educational tool, not tax advice.
