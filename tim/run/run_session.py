"""Drive one scripted session through pi (alone or under ag pi) and save the raw material.

Usage:
  python run/run_session.py --condition pi_compact --session sessions/pilot_seed0 --rep 1 \
      [--max-turns 30] [--results results] [--max-usd 5]

Conditions: pi_compact, pi_per_client, ag_half, ag_quarter, pi_full (see CONDITIONS below).

What it does (layout in TRACE.md):
  - runs a logging proxy in front of the Gemini API. pi's model baseUrl points at it, so every Actor
    call (agent steps and pi's compaction summaries alike) is saved exactly as sent, in every
    condition, to raw/requests/<call_id>.json, with its usage in raw/proxy_calls.jsonl.
    Headers are forwarded but never written anywhere (the API key travels in a header).
  - starts `pi --mode rpc` (or `ag --gatherer <run_id> pi --mode rpc`) per thread and sends user
    message N only after message N-1 has fully finished (agent_end, then not streaming and not
    compacting).
  - copies the agent's deliverables into <run_dir>/deliverables/ after every turn.
  - writes raw/turn_log.jsonl (turn windows, events), run.json.

External tools (pi, the pinned AG clone and its venv) live outside this repo, in BENCH_TOOLS
(default: <repo>/../ag-bench). The agent works in BENCH_TOOLS/work/<run_id>, outside this repo, so
it cannot read the session's answer files. Then run trace/export.py on the run dir.
"""

import argparse
import hashlib
import http.server
import glob
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TIM = os.path.dirname(HERE)
REPO = os.path.dirname(TIM)
TOOLS = os.environ.get("BENCH_TOOLS") or os.path.join(os.path.dirname(REPO), "ag-bench")
PI = os.path.join(TOOLS, "node_modules", ".bin", "pi")
AG = os.path.join(TOOLS, "venv", "bin", "ag")
AG_PY = os.path.join(TOOLS, "venv", "bin", "python")
AG_CLONE = os.path.join(TOOLS, "actor-gatherer")
UPSTREAM = "https://generativelanguage.googleapis.com"

MODEL = "gemini-3.8-flash"
PROVIDER = "gemini-bench"           # pi provider name for our models.json entry
THINKING = "low"                    # pi thinking level for the Actor, same in every condition
CEILING = 100000
# pi compacts past contextWindow - reserveTokens. pi's compaction summary asks for
# maxTokens = 0.8 * reserveTokens, so the ceiling is set by the declared window, not a huge reserve.
RESERVE = 16384
WINDOW = CEILING + RESERVE
TOOLS_ALLOWED = "read,write,edit,ls"

# AG at 59ca484 needs section budgets + keep_pinned_tokens < max_context_tokens. Its defaults sum to
# 105k, so "defaults at 100k" is every budget scaled by 100k/200k, AG's own within_limit rule.
AG_SCALED_100K = {
    "max_context_tokens": 100000, "min_context_tokens": 7500, "keep_pinned_tokens": 2500,
    "all_task_tokens": 15000, "parallel_index_tokens": 5000, "keep_temporal_relevant_tokens": 7500,
    "keep_task_relevant_tokens": 7500, "keep_previous_task_tokens": 7500, "keep_so_far_tokens": 7500,
}
CONDITIONS = {
    "pi_compact": {"ag": False, "per_client": False},
    # pi_full: context-rot reference. No compaction, the model's real window (1,048,576 input tokens),
    # whole session in one thread; no 100k ceiling.
    "pi_full": {"ag": False, "per_client": False, "full": True},
    "pi_per_client": {"ag": False, "per_client": True},
    # ag_half: every AG budget halved (AG's own within_limit rule) so defaults fit under 100k.
    # ag_quarter: as ag_half with the three relevance/previous-task budgets halved again.
    "ag_half": {"ag": True, "per_client": False, "options": dict(AG_SCALED_100K)},
    "ag_quarter": {"ag": True, "per_client": False, "options": {
        **AG_SCALED_100K, "keep_temporal_relevant_tokens": 3750, "keep_task_relevant_tokens": 3750,
        "keep_previous_task_tokens": 3750}},
}

TURN_TIMEOUT_S = 900
MAX_CALLS_PER_TURN = 30


def ag_effective(opts):
    """Every scalar option of AG's policy config at 59ca484 as this run uses it: AG's defaults plus our
    [options]. With max_context_tokens set, AG does not rescale anything (policy.within: max_given)."""
    code = ("import json,sys,dataclasses as d\n"
            "from ag.ag_policies.proactive_context_manager.config import ProactiveContextManagerConfig as C\n"
            "c=d.replace(C(),**json.loads(sys.argv[1]))\n"
            "print(json.dumps({f.name:getattr(c,f.name) for f in d.fields(c) "
            "if isinstance(getattr(c,f.name),(int,float,bool)) or "
            "(isinstance(getattr(c,f.name),str) and len(getattr(c,f.name))<80)}))")
    try:
        r = subprocess.run([AG_PY, "-c", code, json.dumps(opts or {})], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_prices():
    p = json.load(open(os.path.join(HERE, "prices.json")))
    return p["models"][MODEL]


def call_usd(u, price):
    """u: {input, cached, output} tokens (input includes cached)."""
    fresh = max(0, (u.get("input") or 0) - (u.get("cached") or 0))
    return (fresh * price["input"] + (u.get("cached") or 0) * price["cache_read"]
            + (u.get("output") or 0) * price["output"]) / 1e6


# ------------------------------------------------------------------ proxy

class State:
    def __init__(self, raw):
        self.raw = raw
        self.lock = threading.Lock()
        self.turn = None
        self.thread = "main"
        self.seq = 0
        self.actor_usd = 0.0
        self.price = load_prices()
        os.makedirs(os.path.join(raw, "requests"), exist_ok=True)
        os.makedirs(os.path.join(raw, "responses"), exist_ok=True)
        self.log = open(os.path.join(raw, "proxy_calls.jsonl"), "a")


def parse_sse(body):
    """Gemini streamGenerateContent?alt=sse: the last usageMetadata, finish reason, function calls."""
    usage, finish, tools = {}, None, []
    text = body.decode("utf-8", "replace")
    chunks = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            try:
                chunks.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    if not chunks:
        try:
            j = json.loads(text)
            chunks = j if isinstance(j, list) else [j]
        except ValueError:
            pass
    for c in chunks:
        if not isinstance(c, dict):
            continue
        if c.get("usageMetadata"):
            usage = c["usageMetadata"]
        for cand in c.get("candidates") or []:
            finish = cand.get("finishReason") or finish
            for part in (cand.get("content") or {}).get("parts") or []:
                if "functionCall" in part:
                    tools.append(part["functionCall"].get("name"))
    return usage, finish, tools


def make_handler(state):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            with state.lock:
                state.seq += 1
                call_id = f"c{state.seq:05d}"
                turn, thread = state.turn, state.thread
            started, t0 = now(), time.time()
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            sysins = json.dumps((payload or {}).get("systemInstruction") or "")
            purpose = "compaction" if "context summarization assistant" in sysins.lower() else "agent"
            with open(os.path.join(state.raw, "requests", f"{call_id}.json"), "w") as fh:
                json.dump({"call_id": call_id, "turn": turn, "thread": thread, "started_at": started,
                           "purpose": purpose, "path": self.path.split("?")[0], "body": payload}, fh)
            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() in ("content-type", "x-goog-api-key", "accept", "x-goog-api-client")}
            req = urllib.request.Request(UPSTREAM + self.path, data=body, headers=fwd, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    status, rbody, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
            except urllib.error.HTTPError as e:
                status, rbody, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
            except Exception as e:  # network failure: let pi retry
                status, rbody, ctype = 502, json.dumps({"error": {"message": repr(e)}}).encode(), "application/json"
            secs = time.time() - t0
            wait, pending, capped = ag_wait(ag_home) if cond["ag"] else (0.0, [], False)
            with open(os.path.join(state.raw, "responses", f"{call_id}.txt"), "wb") as fh:
                fh.write(rbody)
            usage, finish, tools = parse_sse(rbody) if status == 200 else ({}, None, [])
            u = {"input": usage.get("promptTokenCount", 0), "cached": usage.get("cachedContentTokenCount", 0),
                 "output": usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
                 "thoughts": usage.get("thoughtsTokenCount", 0)}
            usd = call_usd(u, state.price)
            rec = {"call_id": call_id, "turn": turn, "thread": thread, "purpose": purpose,
                   "started_at": started, "seconds": round(secs, 3), "status": status,
                   "finish_reason": finish, "tool_calls": tools, "usage": u, "usd": usd,
                   "error": None if status == 200 else rbody[:500].decode("utf-8", "replace")}
            with state.lock:
                state.actor_usd += usd
                state.log.write(json.dumps(rec) + "\n")
                state.log.flush()
            self.send_response(status)
            self.send_header("Content-Type", ctype or "application/json")
            self.send_header("Content-Length", str(len(rbody)))
            self.end_headers()
            self.wfile.write(rbody)

    return H


class ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ------------------------------------------------------------------ pi over RPC

class PiProc:
    def __init__(self, name, argv, cwd, env, raw):
        self.name = name
        self.events = open(os.path.join(raw, f"rpc_{name}.jsonl"), "a")
        self.err = open(os.path.join(raw, f"stderr_{name}.txt"), "a")
        self.p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.err)
        self.buf = b""
        self.q = []
        self.lock = threading.Condition()
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.n = 0

    def _read(self):
        while True:
            chunk = self.p.stdout.read1(1 << 16)
            if not chunk:
                with self.lock:
                    self.closed = True
                    self.lock.notify_all()
                return
            self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") not in ("message_update", "tool_execution_update"):
                    self.events.write(json.dumps({"t": now(), **e}) + "\n")
                    self.events.flush()
                with self.lock:
                    self.q.append(e)
                    self.lock.notify_all()

    def next(self, timeout):
        with self.lock:
            end = time.time() + timeout
            while not self.q and not self.closed:
                left = end - time.time()
                if left <= 0:
                    return None
                self.lock.wait(left)
            if self.q:
                return self.q.pop(0)
            return {"type": "_closed"}

    def send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def request(self, obj, timeout=60):
        self.n += 1
        rid = f"r{self.n}"
        self.send({**obj, "id": rid})
        end = time.time() + timeout
        while time.time() < end:
            e = self.next(end - time.time())
            if e is None or e.get("type") == "_closed":
                return None
            if e.get("type") == "response" and e.get("id") == rid:
                return e
            self.side(e)
        return None

    def side(self, e):
        """Handle events that need an answer or signal a broken setup."""
        if e.get("type") == "extension_ui_request" and e.get("method") in ("select", "confirm", "input", "editor"):
            self.send({"type": "extension_ui_response", "id": e["id"], "cancelled": True})

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.p.kill()


def run_turn(proc, text, turn):
    """Send one user message; return (events, error). Waits for agent_end and idle."""
    events, err, calls = [], None, 0
    proc.n += 1
    rid = f"p{turn}"
    proc.send({"id": rid, "type": "prompt", "message": text})
    t_end = time.time() + TURN_TIMEOUT_S
    ended = False
    while not ended:
        e = proc.next(max(1, t_end - time.time()))
        if e is None:
            proc.send({"type": "abort"})
            return events + ["error"], f"turn timeout after {TURN_TIMEOUT_S}s"
        t = e.get("type")
        if t == "_closed":
            return events + ["error"], "pi exited"
        proc.side(e)
        if t == "response" and e.get("id") == rid and not e.get("success"):
            return events + ["error"], f"prompt rejected: {e.get('error')}"
        if t == "extension_ui_request" and e.get("method") == "notify" and "context manager" in str(e.get("message")):
            err = f"AG: {e.get('message')}"
        if t == "extension_ui_request" and e.get("method") == "setStatus" and e.get("statusText") == "ag: off":
            err = err or "AG reported ag: off"
        if t == "compaction_start":
            events.append("compaction")
        if t == "auto_retry_start":
            events.append("retry")
        if t == "message_end" and (e.get("message") or {}).get("role") == "assistant":
            calls += 1
            if (e["message"].get("stopReason") == "error"):
                events.append("error")
            if calls >= MAX_CALLS_PER_TURN:
                proc.send({"type": "abort"})
                err = err or f"aborted after {calls} calls in one turn"
        if t == "agent_end":
            ended = True
    # settle: pi may compact after the agent ends
    for _ in range(int(TURN_TIMEOUT_S / 0.5)):
        r = proc.request({"type": "get_state"})
        if r is None:
            return events + ["error"], "pi stopped answering get_state"
        st = r.get("data") or {}
        if not st.get("isCompacting") and not st.get("isStreaming"):
            break
        time.sleep(0.5)
    # events that arrived while settling were consumed by request(); re-scan the log is done by export
    return events, err


# ------------------------------------------------------------------ setup

FULL_WINDOW = 1048576   # gemini-3.8-flash inputTokenLimit (models API, 2026-11-19)


def write_pi_agent_dir(d, port, full=False):
    os.makedirs(d, exist_ok=True)
    models = {"providers": {PROVIDER: {
        "baseUrl": f"http://127.0.0.1:{port}/v1beta", "api": "google-generative-ai", "apiKey": "GEMINI_API_KEY",
        "models": [{"id": MODEL, "name": f"{MODEL} (bench)", "input": ["text"], "contextWindow": FULL_WINDOW if full else WINDOW,
                    "maxTokens": 65536, "reasoning": True,
                    "cost": {"input": 0.75, "output": 3.75, "cacheRead": 0.075, "cacheWrite": 0}}]}}}
    json.dump(models, open(os.path.join(d, "models.json"), "w"), indent=1)
    settings = {"compaction": {"enabled": not full, "reserveTokens": RESERVE, "keepRecentTokens": 20000},
                "retry": {"enabled": True}}
    json.dump(settings, open(os.path.join(d, "settings.json"), "w"), indent=1)


def write_ag_home(home, gatherer, options):
    env = {**os.environ, "AG_HOME": home}
    subprocess.run([AG_PY, "-m", "ag.ag_harness.home", "path"], env=env, check=True, capture_output=True)
    cfg_path = os.path.join(home, "config.toml")
    cfg = open(cfg_path).read()
    assert 'model = ""' in cfg, "unexpected AG config template"
    cfg = cfg.replace('model = ""', f'model = "gemini/{MODEL}"\nlog_prompts = true', 1)
    open(cfg_path, "w").write(cfg)
    gd = os.path.join(home, "gatherers", gatherer)
    os.makedirs(gd, exist_ok=True)
    lines = ['policy = "proactive_context_manager"', 'pollers = ["file_changes"]', "", "[options]"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in options.items()]
    open(os.path.join(gd, "gatherer.toml"), "w").write("\n".join(lines) + "\n")


def git_out(args, cwd):
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout.strip()
    except OSError:
        return None


BUILD_WAIT_CAP_S = 300


def ag_wait(home, cap=BUILD_WAIT_CAP_S):
    """Before the next user message: wait while AG has a boundary check undecided or a build running
    (as a real user's pause would allow), at most `cap` seconds. Returns (seconds waited, what was
    pending at the start, whether the cap was hit)."""
    import sqlite3
    t0, first = time.time(), None
    while True:
        pending = []
        for db in glob.glob(os.path.join(home, "gatherers", "*", "context.db")) + \
                glob.glob(os.path.join(home, "context.db")):
            try:
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                if c.execute("select count(*) from _boundaries where decided_at is null").fetchone()[0]:
                    pending.append("check")
                if c.execute("select count(*) from _builds where status='running' and started_at > ?",
                             (time.time() - 3600,)).fetchone()[0]:
                    pending.append("build")
                c.close()
            except sqlite3.Error:
                pass    # store not created yet, or busy
        if first is None:
            first = pending
        waited = time.time() - t0
        if not pending:
            return round(waited, 3), first, False
        if waited >= cap:
            return round(waited, 3), first, True
        time.sleep(1.0)


def ag_usd_so_far(home, price):
    d = os.path.join(home, "logs", "llm")
    total = 0.0
    if not os.path.isdir(d):
        return 0.0
    for f in os.listdir(d):
        try:
            r = json.load(open(os.path.join(d, f)))
        except (OSError, ValueError):
            continue
        u = r.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        total += call_usd({"input": u.get("prompt_tokens") or 0, "cached": cached,
                           "output": u.get("completion_tokens") or 0}, price)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", required=True, choices=sorted(CONDITIONS))
    ap.add_argument("--session", required=True)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--max-turns", type=int, default=0, help="0 = all")
    ap.add_argument("--results", default=os.path.join(TIM, "results"))
    ap.add_argument("--max-usd", type=float, default=0, help="stop when actor+gatherer spend passes this")
    ap.add_argument("--run-id", default="")
    a = ap.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set")
    cond = CONDITIONS[a.condition]
    session_dir = os.path.abspath(a.session)
    meta = json.load(open(os.path.join(session_dir, "meta.json")))
    msgs = [json.loads(l) for l in open(os.path.join(session_dir, "session.jsonl"))]
    if a.max_turns:
        msgs = msgs[: a.max_turns]
    run_id = a.run_id or f"{a.condition}_seed{meta['seed']}_rep{a.rep}"
    run_dir = os.path.join(os.path.abspath(a.results), run_id)
    if os.path.exists(run_dir):
        sys.exit(f"{run_dir} exists; remove it or pick another --run-id")
    raw = os.path.join(run_dir, "raw")
    # pi puts the working directory in the system prompt: keep the condition name out of it.
    work = os.path.join(TOOLS, "work", hashlib.sha256(run_id.encode()).hexdigest()[:8], "workspace")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(work, "deliverables"))
    os.makedirs(os.path.join(run_dir, "deliverables"))
    os.makedirs(os.path.join(raw, "pi"))

    sp_path = os.path.join(HERE, "system_prompt.md")
    sp_sha = hashlib.sha256(open(sp_path, "rb").read()).hexdigest()
    try:  # pi prints its version to stderr
        r = subprocess.run([PI, "--version"], cwd=TOOLS, capture_output=True, text=True)
        pi_version = (r.stdout.strip() or r.stderr.strip()) or None
    except OSError:
        pi_version = None
    run = {"run_id": run_id, "condition": a.condition, "seed": meta["seed"], "rep": a.rep,
           "session_dir": os.path.relpath(session_dir, TIM), "actor_model": f"gemini/{MODEL}",
           "gatherer_model": f"gemini/{MODEL}" if cond["ag"] else None,
           "ag_commit": git_out(["git", "rev-parse", "--short", "HEAD"], AG_CLONE) if cond["ag"] else None,
           "pi_version": pi_version, "ceiling_tokens": None if cond.get("full") else CEILING, "system_prompt_sha": sp_sha,
           "started_at": now(), "finished_at": None, "status": "partial", "turns_completed": 0, "errors": [],
           "ag_options": cond.get("options"),
           "ag_effective_options": ag_effective(cond.get("options")) if cond["ag"] else None, "actor_thinking": THINKING, "pi_tools": TOOLS_ALLOWED,
           "pi_context_window": FULL_WINDOW if cond.get("full") else WINDOW,
           "pi_compaction": not cond.get("full"), "pi_reserve_tokens": RESERVE, "max_turns": a.max_turns or None,
           "request_capture": "logging proxy in front of the Gemini API (run_session.py)"}

    def save_run():
        json.dump(run, open(os.path.join(run_dir, "run.json"), "w"), indent=1)

    save_run()

    state = State(raw)
    server = ProxyServer(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    agent_dir = os.path.join(raw, "pi", "agent")
    write_pi_agent_dir(agent_dir, port, full=cond.get("full", False))
    ag_home = os.path.join(raw, "ag", "home")
    if cond["ag"]:
        write_ag_home(ag_home, run_id, cond["options"])

    base_env = {**os.environ, "PI_CODING_AGENT_DIR": agent_dir, "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
                "PATH": os.path.join(TOOLS, "node_modules", ".bin") + os.pathsep + os.environ["PATH"]}
    if cond["ag"]:
        base_env.update({"AG_HOME": ag_home, "AG_PYTHON": AG_PY})

    procs = {}

    def get_proc(thread):
        if thread not in procs:
            sess = os.path.join(raw, "pi", thread)
            os.makedirs(sess, exist_ok=True)
            pi_args = ["--mode", "rpc", "--model", f"{PROVIDER}/{MODEL}", "--thinking", THINKING,
                       "--system-prompt", sp_path, "--tools", TOOLS_ALLOWED, "--no-context-files",
                       "--no-skills", "--no-prompt-templates", "--no-themes", "--session-dir", sess]
            argv = [AG, "--gatherer", run_id, "pi", *pi_args] if cond["ag"] else [PI, *pi_args]
            procs[thread] = PiProc(thread, argv, work, base_env, raw)
        return procs[thread]

    turn_log = open(os.path.join(raw, "turn_log.jsonl"), "a")
    price = state.price
    try:
        for m in msgs:
            thread = m["client"] if cond["per_client"] else "main"
            proc = get_proc(thread)
            with state.lock:
                state.turn, state.thread = m["turn"], thread
            started, t0 = now(), time.time()
            events, err = run_turn(proc, m["text"], m["turn"])
            secs = time.time() - t0
            shutil.copytree(os.path.join(work, "deliverables"), os.path.join(run_dir, "deliverables"),
                            dirs_exist_ok=True)
            turn_log.write(json.dumps({"turn": m["turn"], "visit": m["visit"], "client": m["client"],
                                       "thread": thread, "started_at": started, "finished_at": now(),
                                       "seconds": round(secs, 3), "events": events, "error": err,
                                       "ag_wait_seconds": wait, "ag_wait_pending": pending,
                                       "ag_wait_capped": capped}) + "\n")
            turn_log.flush()
            gat = ag_usd_so_far(ag_home, price) if cond["ag"] else 0.0
            spent = state.actor_usd + gat
            print(f"turn {m['turn']:3d} [{thread}] {secs:6.1f}s events={events} "
                  f"actor=${state.actor_usd:.3f} gatherer=${gat:.3f}", flush=True)
            if err:
                run["errors"].append({"turn": m["turn"], "where": thread, "message": err})
                save_run()
                if err.startswith("AG") or "exited" in err or "get_state" in err:
                    run["status"] = "failed"
                    break
            run["turns_completed"] = m["turn"]
            save_run()
            if a.max_usd and spent > a.max_usd:
                run["errors"].append({"turn": m["turn"], "where": "runner",
                                      "message": f"spend cap ${a.max_usd} reached (${spent:.2f})"})
                break
        else:
            run["status"] = "complete"
    except KeyboardInterrupt:
        run["errors"].append({"turn": state.turn, "where": "runner", "message": "interrupted"})
    finally:
        for p in procs.values():
            p.close()
        server.shutdown()
        if a.max_turns and run["status"] == "complete":
            run["status"] = "partial"   # complete for the truncated script only
        run["finished_at"] = now()
        save_run()
        if cond["ag"]:
            # AG builds run in the background and would keep calling the Gatherer model after the
            # session ends (seen in the smoke run: 28 calls over 14 minutes). Stop this run's AG.
            time.sleep(2)
            try:
                r = subprocess.run([AG, "stop"], env=base_env, capture_output=True, text=True, timeout=60)
                run["ag_stop"] = (r.stdout + r.stderr).strip()[:2000]
            except Exception as e:  # noqa: BLE001
                run["ag_stop"] = f"failed: {e}"
            save_run()
    print(f"done: {run_id} status={run['status']} turns={run['turns_completed']} "
          f"actor=${state.actor_usd:.3f}")


if __name__ == "__main__":
    main()
