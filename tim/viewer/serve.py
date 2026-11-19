"""Local trace viewer for long-session runs (reads the normalized TRACE.md layout; stdlib only).

  python viewer/serve.py                          # scans tim/results/ (incl. _synthetic), http://127.0.0.1:8765
  python viewer/serve.py --root results --root /elsewhere/more_runs --port 8765

A run is any folder that contains run.json. The server rescans on every request, so new runs show up
on reload. The "add folder" box in the UI adds a root for the current server session.
Nothing leaves your machine; raw request files can contain the full session text.
"""

import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TIM = os.path.dirname(HERE)
ROOTS = []
TRACE_FILES = ("turns", "calls", "contexts", "decisions", "builds", "gatherer_calls", "messages")
SAFE = re.compile(r"^[\w.\-]+$")
_session_cache = {}


def jl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def jf(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def find_runs():
    runs = {}
    for root in ROOTS:
        for dp, dn, fn in os.walk(root):
            if "run.json" in fn:
                rid = os.path.basename(dp)
                key = rid if rid not in runs else f"{rid} [{os.path.relpath(dp, root)}]"
                runs[key] = dp
                dn[:] = []
            else:
                dn[:] = [d for d in dn if d not in ("raw", "trace", "deliverables", ".git", "__pycache__")]
    return runs


def run_card(rid, d):
    run = jf(os.path.join(d, "run.json"), {})
    summ = jf(os.path.join(d, "summary.json"), {})
    cost = jf(os.path.join(d, "cost.json"), {})
    turns = jl(os.path.join(d, "trace", "turns.jsonl"))
    attr = jl(os.path.join(d, "attribution.jsonl"))
    misses = {"retrieval_miss": 0, "usage_miss": 0}
    for a in attr:
        if a.get("verdict") in misses:
            misses[a["verdict"]] += 1
    secs = sum(t.get("seconds") or 0 for t in turns)
    done = run.get("turns_completed") or (max((t["turn"] for t in turns), default=0))
    grades = jl(os.path.join(d, "grades.jsonl"))
    if grades and run.get("status") != "complete":
        # partial run: score only checks the session actually reached
        rows = [g for g in grades if g.get("kind") != "unknown" and g.get("turn", 0) <= done]
        rate = lambda rs: (sum(r["outcome"] == "pass" for r in rs) / len(rs)) if rs else None
        tags = sorted({t for r in rows for t in r["tags"]})
        unk = [g for g in grades if g.get("kind") == "unknown" and g.get("turn", 0) <= done]
        summ = {"overall": {"pass": rate(rows), "n": len(rows)},
                "by_tag": {t: {"pass": rate([r for r in rows if t in r["tags"]])} for t in tags},
                "by_placement": {p: {"pass": rate([r for r in rows if r["placement"] == p])}
                                 for p in sorted({r["placement"] for r in rows})},
                "unknown_fields": {"made_up_rate": (sum(g["outcome"] == "made_up" for g in unk) / len(unk)) if unk else None}}
    return {"id": rid, "condition": run.get("condition"), "seed": run.get("seed"), "rep": run.get("rep"),
            "status": run.get("status"), "synthetic": bool(run.get("synthetic")),
            "turns_completed": run.get("turns_completed"), "actor_model": run.get("actor_model"),
            "ceiling_tokens": run.get("ceiling_tokens"), "options": run.get("ag_options"),
            "overall": (summ.get("overall") or {}).get("pass"), "n": (summ.get("overall") or {}).get("n"),
            "by_tag": {k: v.get("pass") for k, v in (summ.get("by_tag") or {}).items()},
            "by_placement": {k: v.get("pass") for k, v in (summ.get("by_placement") or {}).items()},
            "made_up_rate": (summ.get("unknown_fields") or {}).get("made_up_rate"),
            "cost": cost, "seconds": round(secs, 1), "turns": len(turns), "misses": misses,
            "has_attribution": bool(attr), "path": d}


def session_for(run):
    sd = run.get("session_dir")
    if not sd:
        return None
    cands = [sd, os.path.join(TIM, sd), os.path.join(os.path.dirname(TIM), sd),
             os.path.join(TIM, "sessions", os.path.basename(sd.rstrip("/")))]
    for c in cands:
        if os.path.exists(os.path.join(c, "session.jsonl")):
            if c not in _session_cache:
                facts = jf(os.path.join(c, "facts.json"), {})
                _session_cache[c] = {
                    "session": jl(os.path.join(c, "session.jsonl")),
                    "checks": jl(os.path.join(c, "checks.jsonl")),
                    "visits": jf(os.path.join(c, "visits.json"), []),
                    "facts": facts}
            return _session_cache[c]
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/add_root":
            n = int(self.headers.get("Content-Length") or 0)
            path = os.path.abspath(os.path.expanduser(json.loads(self.rfile.read(n) or b"{}").get("path", "")))
            if not os.path.isdir(path):
                return self.send({"error": "not a folder: " + path}, 400)
            if path not in ROOTS:
                ROOTS.append(path)
            return self.send({"roots": ROOTS})
        self.send({"error": "not found"}, 404)

    def do_GET(self):
        p = unquote(urlparse(self.path).path)
        if p in ("/", "/index.html"):
            return self.send(open(os.path.join(HERE, "index.html"), "rb").read(), ctype="text/html")
        if p == "/favicon.ico":
            return self.send(b"", 204, "image/x-icon")
        if p == "/api/runs":
            runs = find_runs()
            return self.send({"roots": ROOTS, "runs": [run_card(k, v) for k, v in sorted(runs.items())]})
        m = re.match(r"^/api/run/([^/]+)/(.+)$", p)
        if not m:
            return self.send({"error": "not found"}, 404)
        rid, what = m.group(1), m.group(2)
        runs = find_runs()
        if rid not in runs:
            return self.send({"error": "no such run"}, 404)
        d = runs[rid]
        if what == "all":
            run = jf(os.path.join(d, "run.json"), {})
            out = {"run": run, "summary": jf(os.path.join(d, "summary.json"), {}),
                   "cost": jf(os.path.join(d, "cost.json"), {}),
                   "grades": jl(os.path.join(d, "grades.jsonl")),
                   "attribution": jl(os.path.join(d, "attribution.jsonl")),
                   "coverage": jl(os.path.join(d, "coverage.jsonl"))}
            for t in TRACE_FILES:
                out[t] = jl(os.path.join(d, "trace", t + ".jsonl"))
            return self.send(out)
        if what == "session":
            s = session_for(jf(os.path.join(d, "run.json"), {}))
            return self.send(s if s else {"error": "session not found"}, 200 if s else 404)
        m2 = re.match(r"^request/([\w.\-]+)$", what)
        if m2:
            f = os.path.join(d, "raw", "requests", m2.group(1) + ".json")
            if not os.path.exists(f):
                return self.send({"error": "request file not captured (raw/ is local only)"}, 404)
            return self.send(open(f, "rb").read())
        m4 = re.match(r"^response/([\w.\-]+)$", what)
        if m4:
            for ext in (".txt", ".json", ".sse"):
                f = os.path.join(d, "raw", "responses", m4.group(1) + ext)
                if os.path.exists(f):
                    return self.send(open(f, "rb").read(), ctype="text/plain")
            return self.send({"error": "response not captured (raw/ is local only)"}, 404)
        m3 = re.match(r"^deliverable/([\w.\-]+)$", what)
        if m3:
            f = os.path.join(d, "deliverables", m3.group(1) + ".md")
            if not os.path.exists(f):
                return self.send({"error": "deliverable never written"}, 404)
            return self.send(open(f, "rb").read(), ctype="text/plain")
        self.send({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", help="folder to scan for runs (repeatable)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    for r in (a.root or [os.path.join(TIM, "results")]):
        ROOTS.append(os.path.abspath(os.path.expanduser(r)))
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"viewer on http://{a.host}:{a.port}  roots: {', '.join(ROOTS)}  (runs found: {len(find_runs())})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
