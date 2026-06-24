# DECISIONS

Key design choices and why, for the open items the brief leaves to us. The thesis behind all of them: **a deterministic Python tax core the LLM is structurally forbidden from doing math in** — the model only talks and extracts; code computes the return and fills the real IRS PDF. That makes the four pillars enforced and visible rather than "it's in the prompt."

## The four pillars (the harness — judged first)

Every pillar is enforced in **code**, not merely requested in the prompt:

- **1 · Chat loop** — an explicit finite state machine (not the model) owns the flow and the hard 5-question budget; the LLM only supplies warm wording. The happy path uses 2 of 5 questions. (See #6.)
- **2 · Tools** — a single `dispatch()` is the ONLY code that can mutate a slot, compute the return, or fill the PDF. The model acts solely through typed, schema-validated tool calls; any number it writes in prose is ignored. (See #2–#5.)
- **3 · Guardrails** — a pre-LLM scope gate refuses advice/off-topic input without consuming a question, Pydantic validates every W-2 and slot value, a hard counter caps questions at 5, the W-2 is validated with graceful recovery (an unreadable Box 1 is asked for, not silently zeroed), and download stays locked until `compute_1040` succeeds. (See #7.)
- **4 · Observation** — every turn records an event — tools called (✓/✕), guardrail hits, the decision + its reason, the live question budget, latency, and a SHA-256 audit hash — surfaced in a PII-redacted trace panel **in the UI**, not just the logs.

**Scope note:** core = a single filer with one W-2, fully correct at ~$40k (childless EITC and CTC are both $0 there, so no credit logic is needed). All five filing statuses compute correctly (the standard deduction and brackets differ by status), but for MFJ/MFS we deliberately do **not** collect a spouse's name/SSN — that is left for the filer to add on the form, to protect the 5-question budget. Dependents / CTC / EITC / HoH / QSS credit logic is implemented as a guarded, documented stretch.

1. **Language / framework — Python + FastAPI, single process.** One process serves both the API and its own minimal server-rendered HTML, so there is no build step and no CORS (same-origin). Front-end polish is explicitly not judged, so we spent the complexity budget on the harness and the tax core instead.

2. **LLM provider — OpenRouter (`openai/gpt-4o`) behind a provider-agnostic `llm_turn()`.** The brief allows any provider; we call OpenRouter's OpenAI-compatible `/chat/completions` over plain HTTP, with messages/tools translated behind one function so the orchestrator is provider-agnostic and the model stays swappable. The key is read from `OPENROUTER_API_KEY` at call time (a Cloud Run secret in prod), so the package imports cleanly with no key set.

3. **Deterministic tax core — `compute_1040`, pure Python, no LLM.** The LLM is structurally barred from arithmetic; all math is plain code. TY2025 post-OBBBA constants (Single std deduction $15,750, CTC $2,200) are reused verbatim from a tested source rather than retyped. Line 16 uses the IRS Tax Table midpoint rule (`(taxable//50)*50 + 25`), not the raw bracket formula — the formula is off by $3 against the official table at $40k.

4. **1040 production — fill the official IRS `f1040` AcroForm (229 fields) with `pypdf`.** We fill and flatten the real government form rather than rendering a lookalike. Flattening guarantees the values actually render in Chrome's built-in PDF viewer (the classic "downloads but shows blank" failure), and using the official form is the strongest possible answer to "produce the filled return."

5. **W-2 input — paste text or upload an image, validated with graceful recovery.** The bundled sample takes a deterministic JSON fast-path (no model needed to demo); arbitrary text or images go through LLM vision extraction into a Pydantic-validated schema. Partial or messy input is rejected with a specific, friendly prompt — an unreadable Box 1 asks *"what was the amount in Box 1?"* rather than silently filing a $0 return — then the user re-enters just that field and we re-parse. A $0 in Box 2 is accepted as a legitimate zero.

6. **Conversation design — explicit state machine owns flow; LLM only phrases tone.** A finite state machine, not the model, decides the next action and enforces the hard 5-question budget. The model just supplies warm wording and may only restate tool-computed values. The happy path (clean W-2, single, no dependents) uses 2 of the 5 questions.

7. **Guardrails enforced in code, not just the prompt.** A pre-LLM scope gate refuses off-topic / advice-seeking input (and does not consume a question), Pydantic validates every W-2 and slot value, a hard counter caps information questions at 5, and the download stays locked until `compute_1040` succeeds. The system prompt restates these only as defense in depth.

8. **State / sessions — in-memory, pinned to one Cloud Run instance.** Sessions live in process memory and the service runs with `--min-instances=1 --max-instances=1`, so state is never lost mid-conversation and there is no cold start during judging. Trade-off, stated plainly: no horizontal scale — fine for a single-judge prototype, scale to zero after judging.

9. **Hosting — Google Cloud Run instead of Render.** A comparable free/easy host, and `gcloud` is already installed and authenticated on this machine, so it was the lower-friction path to a public URL with secret management and instance pinning.

10. **Testing — layered, ~2,400 automated cases.** A 2,268-case tax sweep (every filing status × wages $0–$50M × refund/owe/zero, cross-checked against an independent re-derivation of the IRS math), 68 PDF/download-robustness cases ($1 through 12-digit amounts, all statuses, 0–4 dependents, both flatten modes), 54 W-2 validation/recovery cases, plus the core golden + dependent-credit tests. Harness self-tests assert the load-bearing invariants (a prose-only turn with no tool call changes nothing; the scope gate refuses without burning a question), and `scripts/smoke_e2e.py` runs a live end-to-end check against a running instance.

**Not judged / out of scope:** itemized deductions, multiple income types, state tax, e-filing, and real PII. These are asserted/zeroed with a disclaimer — this is an educational tool, not tax advice.
