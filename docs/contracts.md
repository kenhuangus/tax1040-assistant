# FROZEN CONTRACTS — read before writing any code

All shared types live in `app/models.py` (already written; **do not edit it**).
Import everything from there: `from app.models import W2, Identity, TaxFacts,
Dependent, Form1040Result, Event, ToolCall, GuardrailHit, Decision, ScopeResult`.

Money = whole-dollar `int` everywhere. Python 3.13. No `git` commands. Import
style: absolute (`from app.compute import compute_1040`). Each agent owns ONLY
its files (table below) — never edit another agent's file or `app/models.py`.

## Module signatures (frozen)

### `app/taxconstants_2025.py` (agent A)
Plain dict/constants copied verbatim (diff-verified) from
`C:\Users\kenhu\novatax\services\taxConstants\2025.ts`:
`STD_DED`, `BRACKETS` (per status: list of `(upper_limit, rate)`), `CTC=2200`,
`ODC=500`, `CTC_PHASEOUT`, `ACTC_CAP=1700`, `ACTC_FLOOR=2500`, `ACTC_RATE=0.15`,
`EITC` table, `EITC_INVESTMENT_LIMIT=11950`.

### `app/compute.py` (agent A)
- `def compute_1040(facts: TaxFacts) -> Form1040Result`
- Deterministic, NO LLM. Line 16 uses the **IRS Tax Table midpoint rule** for
  taxable < 100000: `base = (taxable//50)*50 + 25`, then bracket math, round
  half-up to whole dollars. Sequence exactly per `docs/architecture.md §4`.
- Set `refund == line_34`, `owed == line_37` (mutually exclusive).
- Golden: single wages=40000, wh=3000 → line_15=24250, line_16=2675, refund=325.

### `app/pdf_fill.py` (agent B)
- `def fill_1040_pdf(result: Form1040Result, identity: Identity, filing_status: str, dependents: list[Dependent], out_path: str, flatten: bool = True) -> str`
- Template: `assets/irs/f1040--2025.pdf`. Field map + on-values: source of truth
  is `assets/irs/_probe/field_spec.json` and `docs/architecture.md §5`.
- Checkbox values MUST include leading `/` (e.g. Single → `/1`). Numbers get
  manual thousands commas. Primary SSN `f1_13` is plain text → format
  `123-45-6789`. Drop `/XFA`. `flatten=True` must produce a PDF whose values are
  visible in Chrome/PDFium. Returns `out_path`.

### `app/w2.py` (agent E)
- `def parse_w2(raw_text: str | None = None, image_b64: str | None = None) -> W2`
- Uses the LLM (anthropic) to extract; validates into `W2`. On bad/partial data
  raise `ValueError` (guardrail catches it). Box 1 → wages, Box 2 → fed_withholding.

### `app/llm.py` (agent E)
- `def llm_turn(system: str, messages: list[dict], tools: list[dict]) -> tuple[list[ToolCall], str]`
- Anthropic `claude-opus-4-8`, tool-use mode. **Never send `temperature`; never
  prefill an assistant turn.** Reads key from env `ANTHROPIC_API_KEY`. Returns
  (tool_calls, assistant_text). Tolerate missing key in tests (raise clearly).

### `app/guardrails.py` (agent E)
- `def scope_check(user_msg: str, state: str) -> ScopeResult` — deterministic,
  pre-LLM. Off-topic / advice-seeking → `ok=False` + fixed refusal
  ("I can only help prepare your 2025 Form 1040 — this is an educational tool,
  not tax advice."). Does NOT consume a question.
- `def redact_ssn(s: str) -> str` → `"XXX-XX-NNNN"` (last 4 kept).

### `app/session.py` (agent E)
- `class Session`: `id:str, slots:Slots, events:list[Event], questions_asked:int,
  repairs:dict[str,int], result:Form1040Result|None, pdf_path:str|None, state:str`.
- `class Slots`: `filing_status:str|None, w2:W2|None, dependents:list[Dependent],
  confirmed:bool`.
- In-memory `STORE: dict[str, Session]`; `def get_or_create(sid: str|None) -> Session`.

### `app/statemachine.py` (agent E)
- States: `AWAIT_W2, AWAIT_FILING_STATUS, AWAIT_DEPENDENTS, CONFIRM_FACTS,
  COMPUTING, READY_DOWNLOAD, REPAIR`.
- `def next_action(session) -> Decision` — pure: looks at filled+valid slots,
  returns one of ASK/CONFIRM/COMPUTE/REPAIR/REFUSE + reason. **Budget gate:** if
  ASK for an unfilled required slot and `questions_asked>=5` → force COMPUTE.
- Question counting: increment ONLY on entry to an ASK state for an unfilled
  required slot. Confirmation, repairs, refusals, retries do NOT count. Repairs
  bounded by `max_repairs_per_slot=2` (separate `session.repairs`).

### `app/tools.py` (agent E — registry + dispatch, P2)
- `TOOLS: list[dict]` — anthropic tool schemas for: `parse_w2`, `set_slot`,
  `compute_1040`, `fill_1040_pdf` (+ optional `correct_slot`).
- `def dispatch(tool_call: ToolCall, session: Session) -> dict` — the ONLY code
  that mutates session slots / triggers compute / fills pdf. Calls into
  `app.w2.parse_w2`, `app.compute.compute_1040`, `app.pdf_fill.fill_1040_pdf`.

### `app/orchestrator.py` (agent E — the loop, P1)
- `def advance(session: Session, user_msg: str) -> dict` returns
  `{reply:str, state:str, questions_asked:int, can_download:bool}`.
- Flow per `architecture.md §2`: scope_check → llm_turn(tool-use) → read ONLY
  tool_use blocks → dispatch each → next_action → budget gate → record Event
  (incl. decision) → return. The model phrases tone only; code owns flow. Never
  read numbers from prose text.

### `app/main.py` (agent E — FastAPI)
- `GET /` → serve `app/ui/index.html` (HTMLResponse; if missing, a minimal inline page).
- `POST /chat` body `{session_id?:str, message:str}` (+ optional W-2 paste/image in message or a separate `POST /upload`) → `{session_id, reply, state, questions_asked, can_download}`.
- `GET /download/{sid}` → the filled PDF (`application/pdf`); **409 until `session.result` exists**. Calls fill on demand if needed.
- `GET /trace/{sid}` → `{events:[...]}` (PII-redacted display events).
- `GET /api/healthz` → `{"status":"ok"}` (dependency-free; NOT `/healthz`).
- `uvicorn app.main:app` must boot.

## Ownership table (disjoint — never cross)
| Agent | Engine | Files |
|---|---|---|
| A | Claude | `app/compute.py`, `app/taxconstants_2025.py`, `tests/test_compute.py` |
| B | Claude | `app/pdf_fill.py`, `tests/test_pdf_fill.py`, `scripts/fill_demo.py` |
| E | Claude | `app/session.py`, `app/statemachine.py`, `app/orchestrator.py`, `app/tools.py`, `app/guardrails.py`, `app/trace.py`, `app/llm.py`, `app/w2.py`, `app/main.py` |
| C | Grok | `Dockerfile`, `requirements.txt`, `.dockerignore`, `.gcloudignore`, `README.md`, `samples/w2_40k.json` |
| D | Grok | `app/ui/index.html` |
| lead | — | `app/models.py`, `app/__init__.py`, integration, all `git` |

## Verify-before-done (each agent runs its own check)
- A: `python -m pytest tests/test_compute.py -q` green; print the single-$40k result.
- B: run `scripts/fill_demo.py` → writes a filled PDF; re-read fields with pypdf and confirm values present; report the output path.
- E: `python -c "import app.main"` imports clean; `python -c "from app.orchestrator import advance"` OK. (LLM calls may be skipped without a key — guard them.)
- C/D: files exist and are well-formed (valid JSON / Dockerfile / HTML).
