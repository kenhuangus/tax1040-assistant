"""
FastAPI app — the single process that serves the chat UI and the harness API.

Endpoints (contract):
  * ``GET  /``               -> the chat UI (app/ui/index.html; inline fallback).
  * ``POST /chat``           -> advance one turn; carries session state across turns.
  * ``POST /upload``         -> upload a W-2 image; routed through parse_w2.
  * ``GET  /download/{sid}`` -> the filled IRS PDF; **409 until result exists**.
  * ``GET  /trace/{sid}``    -> PII-redacted per-turn events (P4 visible surface).
  * ``GET  /api/healthz``    -> {"status":"ok"} (dependency-free; NOT /healthz —
                                Google Frontend reserves /healthz).

Same-origin, no CORS, no build step. In-memory sessions are pinned to one Cloud
Run instance in prod (--min/--max-instances=1) so state survives across turns.

Heavy imports (orchestrator -> llm/tools, which lazily touch compute/pdf_fill)
happen at module load, but compute/pdf_fill themselves are imported lazily inside
dispatch, so ``import app.main`` succeeds even while agents A/B are still writing
those files.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.orchestrator import advance
from app.session import get_or_create
from app.tools import dispatch
from app.trace import display_events

app = FastAPI(title="Agentic Tax-Filing Assistant", version="1.0.0")

_UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"

# Minimal inline page so the app still boots + is demoable if agent D's
# index.html is not present yet. Talks to the same /chat, /trace, /download API.
_INLINE_UI = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>2025 Form 1040 Assistant</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:760px;margin:24px auto;padding:0 12px;color:#16213a}
  h1{font-size:1.25rem}
  #log{border:1px solid #d6dbe6;border-radius:10px;padding:12px;height:48vh;overflow:auto;background:#fafbff}
  .msg{margin:8px 0;padding:8px 10px;border-radius:10px;max-width:80%}
  .user{background:#dce7ff;margin-left:auto}
  .bot{background:#eef1f7}
  #bar{display:flex;gap:8px;margin-top:10px}
  #msg{flex:1;padding:10px;border:1px solid #c8cfdd;border-radius:8px}
  button{padding:10px 14px;border:0;border-radius:8px;background:#2b59ff;color:#fff;cursor:pointer}
  button:disabled{background:#aab4cc;cursor:not-allowed}
  .meta{font-size:.8rem;color:#5b657d;margin-top:6px}
  #trace{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:.75rem;background:#0f1426;color:#cfe0ff;border-radius:10px;padding:10px;margin-top:12px;max-height:32vh;overflow:auto;display:none}
  details summary{cursor:pointer;margin-top:12px;font-weight:600}
</style></head>
<body>
  <h1>2025 Form 1040 Assistant <span style="font-weight:400;font-size:.8rem;color:#5b657d">(educational tool, not tax advice)</span></h1>
  <div id="log"></div>
  <div id="bar">
    <input id="msg" placeholder="Paste your W-2 or type a message…" autocomplete="off">
    <button id="send">Send</button>
    <button id="sample" title="Load a sample ~$40k W-2">Sample</button>
    <button id="dl" disabled>Download</button>
  </div>
  <div class="meta" id="meta">questions: 0/5 · state: AWAIT_W2</div>
  <details><summary>Trace (what the harness saw, decided, did)</summary>
    <div id="trace" style="display:block"></div>
  </details>
<script>
let sid=null;
const log=document.getElementById('log'),meta=document.getElementById('meta'),
      traceEl=document.getElementById('trace'),dl=document.getElementById('dl');
function add(t,who){const d=document.createElement('div');d.className='msg '+who;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function refreshTrace(){if(!sid)return;const r=await fetch('/trace/'+sid);const j=await r.json();traceEl.textContent=JSON.stringify(j.events,null,2);}
async function send(text){
  add(text,'user');
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:sid,message:text})});
  const j=await r.json();sid=j.session_id;
  add(j.reply,'bot');
  meta.textContent='questions: '+j.questions_asked+'/5 · state: '+j.state;
  dl.disabled=!j.can_download;
  refreshTrace();
}
document.getElementById('send').onclick=()=>{const m=document.getElementById('msg');if(m.value.trim()){send(m.value.trim());m.value='';}};
document.getElementById('msg').addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('send').click();});
document.getElementById('sample').onclick=()=>{
  send(JSON.stringify({employee_name:"Alex Sample",ssn:"123-45-6789",address:"100 Main St",city:"Austin",state:"TX",zip:"78701",employer:"Acme Co",wages:40000,fed_withholding:3000}));
};
dl.onclick=()=>{if(sid)window.open('/download/'+sid,'_blank');};
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the chat UI; fall back to a minimal inline page if missing."""
    if _UI_PATH.exists():
        try:
            return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return HTMLResponse(_INLINE_UI)


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str = ""


@app.post("/chat")
def chat(body: ChatIn) -> dict:
    """Advance the conversation one turn. Carries state across turns by sid."""
    session = get_or_create(body.session_id)
    result = advance(session, body.message or "")
    # Echo the (possibly newly-created) session id back so the client pins it.
    return {"session_id": session.id, **result}


@app.post("/upload")
async def upload(session_id: str | None = Form(default=None), file: UploadFile = File(...)) -> dict:
    """Upload a W-2 image; route it through the SAME dispatch -> parse_w2 path.

    Returns the standard chat shape so the UI can treat it like a turn. The image
    is base64-encoded and handed to ``parse_w2`` via ``dispatch`` (P2: the only
    mutating path). We then run an empty ``advance`` so the state machine reacts
    to the now-filled W-2 slot and asks the next question.
    """
    from app.models import ToolCall

    session = get_or_create(session_id)
    raw = await file.read()
    b64 = base64.b64encode(raw).decode("ascii")

    tc = ToolCall(name="parse_w2", args={"image_b64": b64})
    res = dispatch(tc, session)
    if not res.get("ok"):
        # Schema guardrail: surface as a 422 with the reason.
        return JSONResponse(
            status_code=422,
            content={"session_id": session.id, "error": res.get("error", "W-2 could not be read")},
        )
    # Let the state machine advance off the now-filled slot (no LLM needed; an
    # empty user message just triggers next_action). The fallback phrasing asks
    # the next question deterministically if the LLM is unavailable.
    out = advance(session, "")
    return {"session_id": session.id, "parsed": res.get("employee_name"), **out}


@app.get("/download/{sid}")
def download(sid: str):
    """Stream the filled IRS PDF. **409 until compute has produced a result.**

    Fills on demand if the result exists but the PDF hasn't been written yet.
    """
    from app.session import STORE
    from app.tools import _ensure_pdf

    session = STORE.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    if session.result is None:
        # R3.3 + acceptance §4: download is LOCKED until compute_1040 succeeds.
        raise HTTPException(status_code=409, detail="return not computed yet")

    try:
        path = _ensure_pdf(session)
    except Exception as exc:  # pdf_fill not ready (agent B) or fill error
        raise HTTPException(status_code=503, detail=f"PDF not available: {exc}")

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=503, detail="PDF could not be generated")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"form-1040-2025-{sid[:8]}.pdf",
    )


@app.get("/trace/{sid}")
def trace(sid: str) -> dict:
    """Return the PII-redacted per-turn events for a session (P4 visible surface)."""
    from app.session import STORE

    session = STORE.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return {"session_id": sid, "events": display_events(session)}


@app.get("/api/healthz")
def healthz() -> dict:
    """Dependency-free health check (NOT /healthz — GFE reserves that path)."""
    return {"status": "ok"}


__all__ = ["app"]
