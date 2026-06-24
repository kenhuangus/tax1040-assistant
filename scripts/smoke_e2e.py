"""End-to-end smoke test against a running server (http://127.0.0.1:8080).

Drives the real chat flow through the LLM, downloads the filled 1040, and
checks the observation trail + a guardrail. Exit non-zero on failure.
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8080"

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
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.read(), r.status, r.headers.get("Content-Type", "")


def chat(sid, msg):
    r = post("/chat", {"session_id": sid, "message": msg})
    print(f"  state={r.get('state'):<18} q={r.get('questions_asked')}/5 "
          f"dl={r.get('can_download')}  reply: {r.get('reply','')[:90]!r}")
    return r


def main():
    print("== happy path ==")
    r = chat(None, SAMPLE_W2)
    sid = r["session_id"]
    for msg in ["I'm filing as single.", "No dependents — just me.",
                "Yes, please go ahead and fill it out."]:
        r = chat(sid, msg)
        if r.get("can_download"):
            break
    # a couple more nudges if the model is chatty
    tries = 0
    while not r.get("can_download") and tries < 3:
        r = chat(sid, "Yes, go ahead and complete my 1040.")
        tries += 1

    assert r.get("can_download"), "FAIL: never reached can_download"
    assert r.get("questions_asked", 99) <= 5, "FAIL: exceeded 5 questions"

    print("== download ==")
    body, status, ctype = get(f"/download/{sid}")
    assert status == 200 and body[:5] == b"%PDF-", f"FAIL: bad pdf status={status}"
    assert len(body) > 50_000, f"FAIL: pdf too small ({len(body)} bytes)"
    open("out/1040_live.pdf", "wb").write(body)
    print(f"  downloaded {len(body)} bytes, content-type={ctype} -> out/1040_live.pdf")

    # read fields back
    from pypdf import PdfReader
    import io
    f = PdfReader(io.BytesIO(body))
    txt = "".join((p.extract_text() or "") for p in f.pages)
    # sample W-2 withholds 3,120 -> tax 2,675 -> refund 445
    for needle in ["40,000", "24,250", "2,675", "3,120", "445"]:
        assert needle in txt, f"FAIL: expected '{needle}' not visible in PDF text"
    print("  PDF shows wages 40,000 / taxable 24,250 / tax 2,675 / withheld 3,120 / refund 445  OK")

    print("== observation trace ==")
    raw, status, _ = get(f"/trace/{sid}")
    raw_s = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    tr = json.loads(raw_s)
    n = len(tr.get("events", []))
    assert n >= 2, "FAIL: trace empty"
    assert "123-45-6789" not in raw_s, "FAIL: full SSN leaked in trace"
    assert "XXX-XX-6789" in raw_s, "FAIL: redacted SSN not found in trace"
    print(f"  {n} events; full SSN NOT present in trace  OK")

    print("== guardrail (off-topic) ==")
    g = post("/chat", {"session_id": None, "message": "What stocks should I buy this year?"})
    assert g.get("questions_asked", 0) == 0, "FAIL: off-topic consumed a question"
    print(f"  refusal q={g.get('questions_asked')}/5  reply: {g.get('reply','')[:90]!r}  OK")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(e)
        sys.exit(1)
