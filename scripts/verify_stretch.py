"""READ-ONLY stretch/robustness verification against the running app (8080).

Scenarios A-D from the verification brief. Carries session_id across turns,
downloads PDFs, inspects them with pypdf. Does NOT modify app code.
"""
import io
import json
import re
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8080"
OUT = "out"

SAMPLE_W2 = (
    "Here is my W-2:\n"
    "Employee: Jordan A Rivera   SSN: 123-45-6789\n"
    "Address: 742 Birchwood Ave, Columbus, OH 43215\n"
    "Employer: Northwind Logistics LLC (EIN 31-1234567)\n"
    "Box 1 wages: 40000\n"
    "Box 2 federal income tax withheld: 3120\n"
)


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            return r.read(), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.read(), e.code, e.headers.get("Content-Type", "")


def chat(sid, msg, log):
    r = post("/chat", {"session_id": sid, "message": msg})
    line = (f"  [{msg[:40]!r:44}] state={r.get('state'):<18} "
            f"q={r.get('questions_asked')}/5 dl={r.get('can_download')} "
            f"reply={r.get('reply','')[:80]!r}")
    print(line)
    log.append({"msg": msg, **{k: r.get(k) for k in
                ("state", "questions_asked", "can_download", "reply")}})
    return r


def drive_to_download(sid, log, max_nudges=4):
    """Nudge the model toward compute if it's chatty but hasn't unlocked yet."""
    r = log[-1]
    tries = 0
    while not r.get("can_download") and tries < max_nudges:
        r = chat(sid, "Yes, go ahead and complete my 1040.", log)
        tries += 1
    return r


def pdf_text(body):
    from pypdf import PdfReader
    f = PdfReader(io.BytesIO(body))
    return "".join((p.extract_text() or "") for p in f.pages), f


def scenario_A():
    print("\n=== (A) MFJ, no dependents ===")
    log = []
    r = chat(None, SAMPLE_W2, log)
    sid = r["session_id"]
    chat(sid, "married filing jointly", log)
    chat(sid, "no dependents", log)
    r = chat(sid, "yes go ahead", log)
    r = drive_to_download(sid, log)

    res = {"name": "A", "questions": r.get("questions_asked"),
           "can_download": r.get("can_download"), "log": log}
    if not r.get("can_download"):
        res["pass"] = False
        res["note"] = "never reached can_download"
        return res

    body, status, ctype = get(f"/download/{sid}")
    open(f"{OUT}/verify_mfj.pdf", "wb").write(body)
    txt, _ = pdf_text(body)
    res["pdf_status"] = status
    res["pdf_ctype"] = ctype
    res["pdf_bytes"] = len(body)
    res["pdf_is_pdf"] = body[:5] == b"%PDF-"
    # Expected: taxable 8,500 / tax 853 / withheld 3,120 / refund 2,267
    needles = ["8,500", "853", "3,120", "2,267"]
    res["pdf_values"] = {n: (n in txt) for n in needles}
    res["trace_events"] = len(json.loads(get(f"/trace/{sid}")[0].decode()).get("events", []))
    res["pass"] = (status == 200 and res["pdf_is_pdf"] and ctype.startswith("application/pdf")
                   and all(res["pdf_values"].values()) and r.get("questions_asked", 99) <= 5)
    return res


def scenario_B():
    print("\n=== (B) Single, one CTC child (Sam Rivera) ===")
    log = []
    r = chat(None, SAMPLE_W2, log)
    sid = r["session_id"]
    chat(sid, "single", log)
    chat(sid, "I have one child under 17 named Sam Rivera with an SSN of 111-22-3333.", log)
    r = chat(sid, "yes", log)
    r = drive_to_download(sid, log)

    res = {"name": "B", "questions": r.get("questions_asked"),
           "can_download": r.get("can_download"), "log": log}
    if not r.get("can_download"):
        res["pass"] = False
        res["note"] = "never reached can_download"
        return res

    body, status, ctype = get(f"/download/{sid}")
    open(f"{OUT}/verify_ctc.pdf", "wb").write(body)
    txt, reader = pdf_text(body)
    res["pdf_status"] = status
    res["pdf_bytes"] = len(body)
    # Expected: line19 CTC 2,200; taxable 24,250; line24 475; refund 4,312; EITC 1,667
    needles = ["2,200", "24,250", "475", "4,312"]
    res["pdf_values"] = {n: (n in txt) for n in needles}
    res["dep_name_first"] = "Sam" in txt
    res["dep_name_last"] = "Rivera" in txt
    # Flattened PDF: form fields are gone. Detect checkbox glyph near dependents.
    res["form_fields_present"] = bool(reader.get_fields())
    # Look for the dependent SSN digits (comb/plain) and any checkbox-ish mark
    res["dep_ssn_visible"] = ("111-22-3333" in txt) or ("111223333" in txt)
    # crude check: 'X' or check glyph in the page-1 text after the dep name
    res["raw_text_has_X_marks"] = bool(re.search(r"\bX\b", txt))
    res["trace_events"] = len(json.loads(get(f"/trace/{sid}")[0].decode()).get("events", []))
    res["pass"] = (status == 200 and all(res["pdf_values"].values())
                   and res["dep_name_first"] and res["dep_name_last"]
                   and r.get("questions_asked", 99) <= 5)
    return res


def scenario_C():
    print("\n=== (C) Mid-conversation correction (single -> HoH) ===")
    log = []
    r = chat(None, SAMPLE_W2, log)
    sid = r["session_id"]
    r1 = chat(sid, "single", log)
    q_after_single = r1.get("questions_asked")
    r2 = chat(sid, "actually, make that head of household", log)
    q_after_correction = r2.get("questions_asked")

    res = {"name": "C", "log": log,
           "q_after_single": q_after_single,
           "q_after_correction": q_after_correction,
           "state_after_correction": r2.get("state"),
           "reply_after_correction": r2.get("reply")}
    # The correction must NOT increase the question budget.
    res["budget_not_increased"] = (q_after_correction <= q_after_single)
    res["pass"] = res["budget_not_increased"]
    return res


def main():
    print("Health:", get("/api/healthz"))
    results = []
    results.append(scenario_A())
    results.append(scenario_B())
    results.append(scenario_C())

    # (D) Budget: no scenario asked > 5
    maxq = max((r.get("questions") or r.get("q_after_correction") or 0)
               for r in results)
    d_pass = all(
        (s.get("questions") if s.get("questions") is not None
         else s.get("q_after_correction", 0)) <= 5
        for s in results
    )

    print("\n\n================ SUMMARY ================")
    for r in results:
        q = r.get("questions") if r.get("questions") is not None else r.get("q_after_correction")
        print(f"  Scenario {r['name']}: pass={r.get('pass')}  questions={q}")
    print(f"  Scenario D (budget<=5 all): pass={d_pass}  max_questions_seen={maxq}")

    print("\n--- raw results JSON ---")
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
