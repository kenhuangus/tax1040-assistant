# Architecture — Agentic Tax-Filing Assistant (TY2025)

Build-ready technical spec. Companion: [prd.md](./prd.md) (what/why),
[plan.md](./plan.md) (strategy). Repo root: `C:\Users\kenhu\gauntlet\hackathon`.
All field IDs and on-values below were **sentinel-verified against the real PDF**
and are mirrored machine-readably in
`C:\Users\kenhu\gauntlet\hackathon\assets\irs\_probe\field_spec.json`.

---

## 1. Stack & component map
Python 3.13 · FastAPI (one process serving its own minimal HTML — same-origin, no
CORS, no build step) · Anthropic `claude-opus-4-8` (tool-use mode) · `pypdf` for
the AcroForm fill · in-memory sessions (instance-pinned) · Cloud Run.

```
C:\Users\kenhu\gauntlet\hackathon\
├─ app/
│  ├─ main.py            # FastAPI: GET / (chat UI), POST /chat, GET /download/{sid},
│  │                     #          GET /trace/{sid}, GET /api/healthz
│  ├─ orchestrator.py    # P1 chat loop: advance(session, user_msg) -> reply  [the harness brain]
│  ├─ session.py         # P1 typed state: Session, Slots, EventLog (in-memory store)
│  ├─ statemachine.py    # states, transitions, question-counting gate
│  ├─ llm.py             # Anthropic client; tool-use call; NO temperature, NO prefill
│  ├─ tools/
│  │  ├─ registry.py     # P2 the 4 tool schemas + single dispatch(tool_call)
│  │  ├─ parse_w2.py     # text/image -> W2 (Pydantic); LLM-extraction + schema validate
│  │  ├─ compute_1040.py # DETERMINISTIC tax core (no LLM)  [see §4]
│  │  └─ fill_pdf.py      # frozen LINE->field map + pypdf fill+flatten  [see §5]
│  ├─ guardrails.py      # P3 schema + scope gate + budget gate + PII redaction
│  ├─ trace.py           # P4 event model, /trace, hashed-audit vs display surfaces
│  ├─ taxconstants_2025.py  # frozen copy of NovaTax 2025 constants (imported, not retyped)
│  └─ ui/index.html      # minimal chat + collapsible trace panel (core)
├─ assets/irs/f1040--2025.pdf        # official IRS form (229 fields)
├─ assets/irs/_probe/field_spec.json # verified field map (source of truth)
├─ samples/w2_40k.json   # fake ~$40k W-2 fixture (full identity)  [D5]
├─ tests/                # golden tax cases, guardrail tests, integration E2E
├─ Dockerfile  requirements.txt  .dockerignore  .gcloudignore
└─ README.md  docs/{prd,architecture,plan}.md  DECISIONS.md
```

---

## 2. The harness: state machine (P1) + tool dispatch (P2)

**States:** `AWAIT_W2 → AWAIT_FILING_STATUS → AWAIT_DEPENDENTS → CONFIRM_FACTS →
COMPUTING → READY_DOWNLOAD`; plus `REPAIR(slot)` and global `CORRECTION` (stretch).

**`advance(session, user_msg)` per turn:**
1. `guardrails.scope_check(user_msg, session.state)` → if off-topic, emit `guardrail_hit`, return fixed refusal, **no LLM content call, no counter change** (R3.2).
2. Call `llm.turn(...)` in **tool-use mode**. Read **only `tool_use` blocks**. Ignore numbers in prose (R2.2).
3. For each tool_use → `registry.dispatch(tool_call, session)` — the **only** state-mutating path.
4. State machine computes `next_action ∈ {ASK, CONFIRM, COMPUTE, REPAIR, REFUSE}` from which required slots are filled+valid.
5. **Budget gate:** if `next_action==ASK` for an unfilled required slot → `if session.questions_asked >= 5: next_action = COMPUTE` else `questions_asked += 1`. Counting rule per [prd §4]. Repairs use `max_repairs_per_slot=2`, separate counter.
6. Append the turn event (incl. `decision{state_before,state_after,next_action,reason}`) to the log (P4).
7. Return the assistant message (model phrases tone; code owns flow).

**Required slots:** `filing_status`, `w2.wages`, `w2.fed_withholding` (+ identity from W-2). `dependents` is required-but-defaultable (0).

**Tool contracts (registry.py):**
| Tool | Input (validated) | Output | Mutates |
|---|---|---|---|
| `parse_w2` | `{raw_text?, image?}` | `W2{employee_name, ssn, address, wages, fed_withholding, ...}` | sets W-2 slots |
| `set_slot` | `{name, value}` | `{ok, normalized}` | one slot (schema-validated) |
| `compute_1040` | `{facts}` | `Form1040Result` (all line values) | sets `result`, unlocks download |
| `fill_1040_pdf` | `{result}` | `{path}` | writes session PDF |
| `correct_slot`*(stretch)* | `{name, new_value}` | `{ok, old, new}` | overwrites slot, **no counter++** |

---

## 3. Guardrails (P3) — code-enforced, visible
- **Schema:** Pydantic `W2` and `Slots`; reject negative/non-int money, unknown filing status, out-of-range. Rejected parse → `guardrail_hit{rule:"schema"}`.
- **Scope gate (deterministic, pre-LLM):** if current slot's validator rejects the input as off-topic, or input matches advice-seeking intent, emit refusal + disclaimer; don't consume a question; don't call the model for content. Keep a prompt instruction too (defense in depth) but enforcement is the code gate.
- **Budget gate:** §2 step 5. Visible as `{budget:"n/5"}` each turn.
- **Compute/download gating:** download endpoint returns 409 until `session.result` exists.
- **PII:** `redact_ssn("123-45-6789") -> "XXX-XX-NNNN"`. Trace/display events use redacted values; only `fill_pdf` sees the real SSN. (Reuse NovaTax `piiRedaction` pattern.)
- **Domain guardrails (stretch):** HoH/QSS require ≥1 dependent (else refuse/repair); MFS → EITC 0 + spouse-itemizes note; block age/blind/itemized as out-of-scope.

---

## 4. `compute_1040` — deterministic spec (implement directly, no LLM)
**Inputs (Pydantic):** `filing_status: Literal["single","mfj","mfs","hoh","qss"]`,
`w2_wages:int≥0`, `fed_withholding:int≥0`, `dependents: list[Dependent]` (each
`{name, ssn, relationship, is_under_17:bool, has_ssn:bool}`; empty for core).

**Constants:** import frozen from `taxconstants_2025.py` (copied from NovaTax,
diff-verified): `STD_DED={single:15750,mfj:31500,mfs:15750,hoh:23625,qss:31500}`;
bracket tables per status; `CTC=2200, ODC=500, CTC_PHASEOUT={single:200000,
mfj:400000,...}`; `ACTC_CAP=1700, ACTC_FLOOR=2500, ACTC_RATE=0.15`; EITC table;
`EITC_INVESTMENT_LIMIT=11950`.

**`tax(taxable, status)`** (Line 16):
```python
def tax(taxable:int, status:str)->int:
    if taxable <= 0: return 0
    base = taxable if taxable >= 100_000 else ((taxable//50)*50 + 25)  # IRS Tax Table midpoint
    return round_half_up(bracket_tax(base, status))                    # whole dollars
# bracket_tax walks the marginal brackets for `status`.
```

**Sequence (each → a 1040 line; round each money line, half-up):**
1. `1a = 1z = 9 = wages`; `11 (AGI) = wages` (no adjustments).
2. `12 = STD_DED[status]` (assert not claimed-as-dependent for this profile).
3. `13 = 0`; `14 = 12 + 13`; `15 (taxable) = max(0, 11 - 14)`.
4. `16 = tax(15, status)`; `17 = 0`; `18 = 16`.
5. Credits (core: 0). Stretch: `qc=#(under_17 & has_ssn)`, `od=#(rest)`; phaseout only if AGI>threshold (not at $40k); `19 = min(qc*2200 + od*500, 18)`; `28 = min(0.15*max(0,wages-2500), 1700*qc, leftover_ctc)` (ACTC).
6. `20=0; 21=19+20; 22=max(0,18-21); 23=0; 24=22+23` (total tax).
7. EITC (stretch, with-children): `27 = eitc(wages, status, qc)` if `status!="mfs"` and investment≤11950 else 0.
8. `25a=25d=fed_withholding; 32=27+28; 33=25d+32` (total payments).
9. If `33>24`: `34=35a=33-24` (refund), `37=0`; else `37=24-33` (owe), `34=0`. **Exactly one nonzero.**

**Golden tests (must pass):** Single $40k/$3,000 withheld → taxable $24,250, L16 **$2,675**, refund **$325**. MFJ $40k → taxable $8,500, L16 ~$850. HoH (with 1 dep) and MFS variants. Plus the prose-only-no-tool test (P2) and budget-cap test (P3).

---

## 5. `fill_1040_pdf` — verified field map + render-safe recipe
Template: `C:\Users\kenhu\gauntlet\hackathon\assets\irs\f1040--2025.pdf`
(2 pages, 229 fields, **0 tooltips**, hybrid XFA present). Names are
fully-qualified; prefix `P1 = topmostSubform[0].Page1[0].`,
`P2 = topmostSubform[0].Page2[0].`. **Full map of source of truth:**
`assets\irs\_probe\field_spec.json`.

**⚠️ Checkbox on-values must include the leading `/` and match the AP/N key** —
`"1"`, `True`, `"Yes"` silently render Off. Filing status (set exactly one):

| Status | Field (full) | ON |
|---|---|---|
| Single | `P1 Checkbox_ReadOrder[0].c1_8[0]` | `/1` |
| MFJ | `P1 Checkbox_ReadOrder[0].c1_8[1]` | `/2` |
| MFS | `P1 Checkbox_ReadOrder[0].c1_8[2]` | `/3` |
| HoH | `P1 c1_8[0]` (bare) | `/4` |
| QSS | `P1 c1_8[1]` (bare) | `/5` |

(Note the trap: two different fields are named `c1_8[0]` / `c1_8[1]`; the parent
path disambiguates — use the full names.)

**Dollar lines (HIGH confidence, sentinel-verified):**
| Line | Field | Line | Field |
|---|---|---|---|
| 1a wages | `P1 f1_47[0]` | 22 | `P2 f2_14[0]` |
| 1z | `P1 f1_57[0]` | 24 total tax | `P2 f2_16[0]` |
| 9 | `P1 f1_73[0]` | 25a withholding | `P2 f2_17[0]` |
| 11 AGI (p1) | `P1 f1_75[0]` | 25d | `P2 f2_20[0]` |
| 11 AGI (p2) | `P2 f2_01[0]` | 33 total pmts | `P2 f2_29[0]` |
| 12 std ded | `P2 f2_02[0]` | 34 refund | `P2 f2_30[0]` |
| 13 QBI | `P2 f2_03[0]` | 35a refunded | `P2 f2_31[0]` |
| 15 taxable | `P2 f2_06[0]` | 37 amount owed | `P2 f2_35[0]` |
| 16 tax | `P2 f2_08[0]` | 19 CTC | `P2 f2_11[0]` |

**Identity:** `P1 f1_11`=first/MI, `f1_12`=last, **`f1_13`=your SSN (PLAIN text — format `123-45-6789` yourself)**, `Address_ReadOrder[0].f1_20`=street, `f1_22`=city, `f1_23`=state, `f1_24`=zip. Dependents table + age/blind boxes: see `field_spec.json` (`dependents`, `dependent_status_block`).

**Render-safe fill recipe (verified to show values in Chrome/PDFium):**
```python
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, ArrayObject
reader = PdfReader(TEMPLATE); writer = PdfWriter(clone_from=reader)  # clone keeps /DR fonts
acro = writer._root_object["/AcroForm"].get_object()
if "/XFA" in acro: del acro[NameObject("/XFA")]                      # drop hybrid XFA
for page in writer.pages:
    writer.update_page_form_field_values(page, FIELD_VALUES, auto_regenerate=False)
writer.set_need_appearances_writer(True)                            # helps Acrobat; harmless for Chrome
# Download artifact: also produce a FLATTENED copy (bulletproof — renders identically everywhere):
#   update_page_form_field_values(..., flatten=True); strip /Widget annots; del /AcroForm
```
Key facts (proven): pypdf writes per-widget `/AP /N` appearance streams →
Chrome shows the values (NeedAppearances is irrelevant to Chrome). **If we ship
one file, ship the flattened one.** Numbers need **manual thousands commas**
(`"15,750"`), right-aligned automatically. Comb fields (spouse SSN `f1_16`, dep-3
SSN, routing/account) take **digits only, no separators**. Enter `"0"` where the
form says "if zero, -0-".

---

## 6. Deployment runbook (Google Cloud Run — single service)
Honors machine GCP lessons: Secret Manager IAM not automatic; GFE reserves
`/healthz`; `gcloud builds submit --file` removed (use `--source .`).

**Dockerfile:**
```dockerfile
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}   # shell form so $PORT expands
```
**requirements.txt:** `fastapi`, `uvicorn[standard]`, `anthropic`, `pypdf`,
`pydantic`, **`python-multipart`** (required for W-2 upload — app crashes on boot
without it), `Pillow` (only if rasterizing W-2 images). Pin majors.

**Health:** `GET /api/healthz -> {"status":"ok"}` (dependency-free; **not** `/healthz`).

**One-time GCP setup (PowerShell; key read from env, never printed):**
```powershell
$PROJECT = gcloud config get-value project
$RUNTIME_SA = "$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
gcloud services enable run.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
$env:ANTHROPIC_API_KEY | gcloud secrets create anthropic-api-key --data-file=- --replication-policy=automatic
gcloud secrets add-iam-policy-binding anthropic-api-key --member="serviceAccount:$RUNTIME_SA" --role="roles/secretmanager.secretAccessor"  # NOT automatic
```
**Deploy (from repo root):**
```powershell
gcloud run deploy tax1040 --source . --region us-central1 --allow-unauthenticated `
  --min-instances=1 --max-instances=1 --no-cpu-throttling --cpu-boost `
  --memory=512Mi --cpu=1 --port=8080 --timeout=300 `
  --set-secrets="ANTHROPIC_API_KEY=anthropic-api-key:latest"
$URL = gcloud run services describe tax1040 --region us-central1 --format="value(status.url)"
```
- `--min-instances=1 --max-instances=1` → in-memory sessions never lost mid-chat **and** no 30–60s cold start (the two reasons the demo would look broken). Trade-off (~$10–15/mo if left up) noted in DECISIONS; scale to 0 after judging.
- `--allow-unauthenticated` → public; fallback `gcloud run services add-iam-policy-binding tax1040 --region us-central1 --member=allUsers --role=roles/run.invoker`.
- **Smoke:** `curl $URL/api/healthz` (200), `curl $URL/` (200). If `/chat` 500s but health is 200 → the secret IAM grant.

**Repo (D1, currently a gap — no remote):**
```powershell
# verify .env* gitignored and no key tracked, then:
gh repo create tax1040-assistant --public --source . --remote origin --push
```
**Local fallback (D3):** `docker run --rm -p 8080:8080 -e ANTHROPIC_API_KEY=$env:ANTHROPIC_API_KEY tax1040`  **or**  `pip install -r requirements.txt; uvicorn app.main:app --host 0.0.0.0 --port 8080`. Same env-var code path as prod.

**.gcloudignore / .gitignore:** exclude `.git/ .venv/ __pycache__/ .env* .claude/ .fusion/ notes/ tests/`; **keep `assets/` in the build context** (the PDF must ship in the image).

---

## 7. Build order (de-risk first)
0. **PDF fill** — freeze the field map from `field_spec.json`, prove a hand-filled 1040 downloads and renders in Chrome (flattened). *Highest risk → first.*
1. **`compute_1040`** + golden tests (Tax-Table rule).
2. **FastAPI + state machine + dispatch + tools** wired to 0–1 → happy-path E2E (no LLM yet; drive with canned tool calls).
3. **LLM layer** — tool-use conversation + W-2 extraction; tone prompt.
4. **Guardrails + trace** (scope gate, budget gate, decision field, UI panel).
5. **Deploy** to Cloud Run + GitHub push + DECISIONS.md.
6. **Browser-agent E2E gate** against the live URL (prd §5). Fix → redeploy → re-run full walk until N/N pass.
