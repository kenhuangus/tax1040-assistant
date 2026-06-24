# Agentic Tax-Filing Assistant — IRS Form 1040 (TY2025)

A web chat where a person with a single ~$40k W-2 has a short, warm conversation
(≤ 5 questions) and downloads a correctly filled **2025 IRS Form 1040**. Built on
a harness that demonstrates four pillars, with a **deterministic tax core the LLM
is structurally forbidden from doing math in**.

> Educational hackathon project — **not tax advice**, fake data only, no e-filing.

## The four pillars
- **Chat loop** — server-held typed session state advanced by an explicit state machine each turn (`app/orchestrator.py`, `app/session.py`).
- **Tools** — the agent acts only through typed tools; a single `dispatch()` is the one path that mutates state / fills the return (`app/tools.py`).
- **Guardrails** — code-enforced scope refusal, Pydantic schema validation, and a hard 5-question budget; download locks until compute succeeds (`app/guardrails.py`, `app/statemachine.py`).
- **Observation** — every turn records a structured event (tool calls, slot changes, guardrail hits, decision + reason); visible via `GET /trace/{id}` and the UI trace panel (`app/trace.py`).

## Live URL
**TBD — Google Cloud Run** (see `docs/architecture.md` §6 to deploy).

## Run locally (one command)
Set your key first: `export ANTHROPIC_API_KEY=...` (Windows: `$env:ANTHROPIC_API_KEY=...`).

**Docker:**
```
docker build -t tax1040 .
docker run --rm -p 8080:8080 -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY tax1040
```
**No Docker:**
```
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
Then open http://localhost:8080 and click **Load sample W-2** to try it.

## Tests
```
pytest -q
```

## Docs
- `docs/prd.md` — requirements & the four pillars as acceptance criteria
- `docs/architecture.md` — components, tax compute spec, 1040 field map, deploy runbook
- `docs/contracts.md` — frozen module interfaces
- `DECISIONS.md` — key design choices and why
