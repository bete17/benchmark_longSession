"""Turn a run's raw material into the normalized trace of TRACE.md.

Usage: python trace/export.py --run results/<run_id> [--session sessions/pilot_seed0]

Reads (all under <run>/raw/):
  proxy_calls.jsonl, requests/<call_id>.json   every Actor call, as sent (run_session.py's proxy)
  turn_log.jsonl                               turn windows
  rpc_<thread>.jsonl                           pi's RPC event stream (compaction events)
  pi/<thread>/*.jsonl                          pi session files (compaction entries)
  ag/home/gatherers/<run_id>/context.db        AG's store (AG runs)
  ag/home/logs/llm/*.json                      AG's own model calls (AG runs)
Writes: trace/{turns,calls,contexts,decisions,builds,gatherer_calls}.jsonl and cost.json.

Item token counts in contexts.jsonl: AG items carry AG's own counts (its tokenizer, or the provider's
when AG recorded them); pi items are counted with tiktoken cl100k_base when importable, else chars/4.
`total_tokens` is always the provider's input token count for the call.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TIM = os.path.dirname(HERE)
COMPACTION_PREFIX = "The conversation history before this point was compacted into the following summary"

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def count(text):
        return len(_enc.encode(text or "", disallowed_special=()))
    COUNTER = "tiktoken cl100k_base"
except Exception:  # noqa: BLE001
    def count(text):
        return len(text or "") // 4
    COUNTER = "chars/4"


def ts(s):
    """ISO string or epoch seconds/ms -> epoch seconds."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s / 1000 if s > 1e11 else float(s)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def jl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def write_jl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def usd(inp, cached, out, price):
    return (max(0, inp - cached) * price["input"] + cached * price["cache_read"] + out * price["output"]) / 1e6


def turn_at(t, windows):
    """The turn whose window [start, next start) holds epoch t."""
    best = None
    for w in windows:
        if w["start"] <= t:
            best = w["turn"]
        else:
            break
    return best


# ------------------------------------------------------------------ pi request parsing

def part_text(p):
    if "text" in p:
        return p["text"]
    if "functionCall" in p:
        return json.dumps(p["functionCall"])
    if "functionResponse" in p:
        return json.dumps(p["functionResponse"])
    return json.dumps(p)


def match_turn(txt, text_to_turns, last, call_turn):
    """Short messages recur verbatim in the script: take the first candidate after the last matched turn,
    not after the call's turn."""
    cands = [t for t in text_to_turns.get(txt.strip(), []) if call_turn is None or t <= call_turn]
    if not cands:
        return None
    later = [t for t in cands if last is None or t > last]
    return later[0] if later else cands[-1]


def items_from_request(body, text_to_turns, call_turn):
    items = []
    last = None
    si = body.get("systemInstruction")
    if si:
        txt = "".join(part_text(p) for p in si.get("parts", []))
        items.append({"kind": "system", "origin_turn": None, "role": "system", "tokens": count(txt), "label": "system prompt"})
    tools = body.get("tools")
    if tools:
        items.append({"kind": "system", "origin_turn": None, "role": "system", "tokens": count(json.dumps(tools)),
                      "label": "tool definitions"})
    cur = None
    for c in body.get("contents", []):
        parts = c.get("parts", [])
        txt = "".join(part_text(p) for p in parts)
        if c.get("role") == "user" and parts and all("text" in p for p in parts):
            if txt.startswith(COMPACTION_PREFIX):
                items.append({"kind": "summary", "origin_turn": None, "role": "user", "tokens": count(txt),
                              "label": "compaction summary"})
                cur = None
                continue
            t = match_turn(txt, text_to_turns, last, call_turn)
            if t is not None:
                cur = last = t
                items.append({"kind": "message", "origin_turn": t, "role": "user", "tokens": count(txt), "label": None})
                continue
            items.append({"kind": "message", "origin_turn": None, "role": "user", "tokens": count(txt),
                          "label": "unmatched user text"})
            continue
        role = "tool" if any("functionResponse" in p for p in parts) else "assistant"
        items.append({"kind": "message", "origin_turn": cur, "role": role, "tokens": count(txt), "label": None})
    return items


# ------------------------------------------------------------------ AG store

def ag_store(raw, run_id):
    dbs = glob.glob(os.path.join(raw, "ag", "home", "gatherers", "*", "context.db"))
    if not dbs:
        return None
    conn = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def ag_row_turns(conn):
    """events.row_id -> turn: the n-th user_message is turn n; other rows take the turn of the
    latest user message before them."""
    rows = conn.execute("select e.row_id, t.name from events e join event_types t using(event_type_id) "
                        "order by e.row_id").fetchall()
    m, turn = {}, 0
    for r in rows:
        if r["name"] == "user_message":
            turn += 1
        m[r["row_id"]] = turn or None
    return m, {r["row_id"]: r["name"] for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--session", default=None)
    a = ap.parse_args()
    run_dir = os.path.abspath(a.run)
    raw = os.path.join(run_dir, "raw")
    run = json.load(open(os.path.join(run_dir, "run.json")))
    session_dir = a.session or os.path.join(TIM, run["session_dir"])
    msgs = {m["turn"]: m for m in jl(os.path.join(session_dir, "session.jsonl"))}
    text_to_turns = defaultdict(list)
    for t in sorted(msgs):
        text_to_turns[msgs[t]["text"].strip()].append(t)
    prices = json.load(open(os.path.join(TIM, "run", "prices.json")))["models"]
    actor_price = prices[run["actor_model"].split("/")[-1]]
    gat_price = prices[run["gatherer_model"].split("/")[-1]] if run.get("gatherer_model") else None
    out = os.path.join(run_dir, "trace")
    os.makedirs(out, exist_ok=True)

    tlog = jl(os.path.join(raw, "turn_log.jsonl"))
    windows = sorted(({"turn": r["turn"], "start": ts(r["started_at"]), "end": ts(r["finished_at"])} for r in tlog),
                     key=lambda w: w["start"])
    proxy = jl(os.path.join(raw, "proxy_calls.jsonl"))

    # ---- calls.jsonl
    calls, idx = [], defaultdict(int)
    for p in proxy:
        key = (p["turn"], p["thread"])
        u = p["usage"]
        calls.append({"call_id": p["call_id"], "turn": p["turn"], "thread": p["thread"],
                      "index_in_turn": idx[key], "started_at": p["started_at"], "seconds": p["seconds"],
                      "input_tokens": u["input"], "output_tokens": u["output"], "cache_tokens": u["cached"],
                      "stop_reason": p["finish_reason"] if p["status"] == 200 else f"http_{p['status']}",
                      "tool_calls": p["tool_calls"], "request_file": f"raw/requests/{p['call_id']}.json",
                      "purpose": p["purpose"], "usd": p["usd"]})
        idx[key] += 1
    write_jl(os.path.join(out, "calls.jsonl"), calls)

    conn = ag_store(raw, run["run_id"]) if run.get("gatherer_model") else None
    decisions, builds_out, gcalls = [], [], []

    # ---- contexts.jsonl
    contexts = []
    if conn is not None:
        row_turn, row_kind = ag_row_turns(conn)
        reads = conn.execute("select * from _reads order by read_id").fetchall()
        agent_calls = [c for c in calls if c["purpose"] == "agent" and c["input_tokens"]]
        made = {r["made_id"]: r for r in conn.execute("select made_id, name, tokens from _made_items")}
        pre = {r["preamble_id"]: r for r in conn.execute("select preamble_id, name, tokens from _preambles")}
        if len(reads) != len(agent_calls):
            print(f"warning: {len(reads)} AG reads vs {len(agent_calls)} agent calls; pairing in order")
        for c, rd in zip(agent_calls, reads):
            items = []
            for pid in json.loads(rd["preamble_json"] or "[]"):
                p = pre.get(pid)
                if p:
                    items.append({"kind": "system", "origin_turn": None, "role": "system", "tokens": p["tokens"],
                                  "label": p["name"]})
            for it in conn.execute("select position, row_id, made_id from _read_items where read_id=? order by position",
                                   (rd["read_id"],)):
                if it["made_id"] is not None:
                    mi = made.get(it["made_id"])
                    items.append({"kind": "summary", "origin_turn": None, "role": "user",
                                  "tokens": mi["tokens"] if mi else None, "label": mi["name"] if mi else None})
                else:
                    ev = conn.execute("select tokens from events where row_id=?", (it["row_id"],)).fetchone()
                    k = row_kind.get(it["row_id"])
                    role = {"user_message": "user", "tool_output": "tool"}.get(k, "assistant")
                    items.append({"kind": "message", "origin_turn": row_turn.get(it["row_id"]), "role": role,
                                  "tokens": ev["tokens"] if ev else None, "label": k})
            contexts.append({"call_id": c["call_id"], "turn": c["turn"], "total_tokens": c["input_tokens"],
                             "items": items, "ag_read_id": rd["read_id"], "ag_basis": rd["basis"]})
    else:
        for c in calls:
            if c["purpose"] != "agent":
                continue
            req = json.load(open(os.path.join(raw, "requests", f"{c['call_id']}.json")))
            contexts.append({"call_id": c["call_id"], "turn": c["turn"], "total_tokens": c["input_tokens"],
                             "items": items_from_request(req["body"] or {}, text_to_turns, c["turn"])})
    # pi compaction summary calls (any condition): one row each, so every Actor call has a context row
    for c in calls:
        if c["purpose"] != "compaction":
            continue
        req = json.load(open(os.path.join(raw, "requests", f"{c['call_id']}.json")))
        items = items_from_request(req["body"] or {}, {}, c["turn"])
        for it in items:
            if it["label"] == "system prompt":
                it["label"] = "compaction instructions"
            elif it["label"] == "unmatched user text":
                it["label"] = "conversation to summarize"
        contexts.append({"call_id": c["call_id"], "turn": c["turn"], "total_tokens": c["input_tokens"],
                         "items": items})
    contexts.sort(key=lambda r: r["call_id"])
    write_jl(os.path.join(out, "contexts.jsonl"), contexts)

    # ---- pi compactions (any condition; under AG pi's compaction is normally cancelled)
    first_call_after = {}
    for c in calls:
        if c["purpose"] == "agent":
            first_call_after.setdefault(c["thread"], []).append((ts(c["started_at"]), c["input_tokens"]))
    compaction_turns = defaultdict(int)
    for sf in sorted(glob.glob(os.path.join(raw, "pi", "*", "*.jsonl"))):
        thread = os.path.basename(os.path.dirname(sf))
        for e in jl(sf):
            if e.get("type") != "compaction":
                continue
            t = ts(e.get("timestamp"))
            turn = turn_at(t, windows) if t else None
            after = next((tok for (st, tok) in first_call_after.get(thread, []) if st > t), None) if t else None
            decisions.append({"turn": turn, "kind": "compaction",
                              "detail": {"thread": thread, "tokens_before": e.get("tokensBefore"),
                                         "tokens_after": after, "summary_chars": len(e.get("summary") or ""),
                                         "first_kept_entry_id": e.get("firstKeptEntryId")}})
            compaction_turns[turn] += 1

    # ---- AG decisions, builds, gatherer calls
    if conn is not None:
        for b in conn.execute("select row_id, new_task, why, asked_at, decided_at from _boundaries order by row_id"):
            decisions.append({"turn": row_turn.get(b["row_id"]), "kind": "boundary",
                              "detail": {"new_task": b["new_task"], "why": b["why"], "row_id": b["row_id"],
                                         "seconds": round(b["decided_at"] - b["asked_at"], 3) if b["decided_at"] else None}})
        tasks = [dict(r) for r in conn.execute("select * from tasks order by channel_key, task_index")]
        reads_by_basis = defaultdict(list)
        for rd in conn.execute("select read_id, basis from _reads where basis is not null order by read_id"):
            reads_by_basis[str(rd["basis"])].append(rd["read_id"])
        all_builds = conn.execute("select * from _builds order by build_id").fetchall()
        for b in all_builds:
            turn = turn_at(b["started_at"], windows)
            secs = round(b["finished_at"] - b["started_at"], 3) if b["finished_at"] else None
            through = row_turn.get(b["through_row"]) if b["through_row"] else None
            open_from = row_turn.get(b["open_from_row"]) if b["open_from_row"] else None
            decisions.append({"turn": turn, "kind": "build",
                              "detail": {"build_id": b["build_id"], "status": b["status"], "seconds": secs,
                                         "through_turn": through, "open_from_turn": open_from,
                                         "forced": b["reason"] == "limit", "reason": b["reason"], "error": b["error"]}})
            sections = []
            adopted = reads_by_basis.get(str(b["build_id"]))
            if adopted:
                for it in conn.execute("select m.name, m.tokens from _read_items i join _made_items m using(made_id) "
                                       "where i.read_id=? order by i.position", (adopted[0],)):
                    sections.append({"name": it["name"], "tokens": it["tokens"]})
            is_last = b["build_id"] == all_builds[-1]["build_id"]
            ranking = json.loads(b["ranking_json"]) if b["ranking_json"] else None
            builds_out.append({
                "build_id": b["build_id"], "turn": turn, "status": b["status"], "seconds": secs,
                "open_from_turn": open_from, "reason": b["reason"],
                # the tasks table keeps only the latest segmentation: tasks are given for the last build only
                "tasks": [{"index": t["task_index"], "name": t["name"], "description": t["description"],
                           "first_turn": row_turn.get(t["first_row"]), "last_turn": row_turn.get(t["last_row"]),
                           "tokens": t["tokens"],
                           "retention": json.loads(t["retention_json"]) if t["retention_json"] else None}
                          for t in tasks] if is_last else None,
                "ranking": [{"channel": r[0], "task_index": r[1]} for r in ranking] if ranking else ranking,
                "sections": sections,
                "adopted_by_read": adopted[0] if adopted else None})
        write_jl(os.path.join(out, "builds.jsonl"), builds_out)

        for f in sorted(glob.glob(os.path.join(raw, "ag", "home", "logs", "llm", "*.json"))):
            try:
                r = json.load(open(f))
            except (OSError, ValueError):
                continue
            u = r.get("usage") or {}
            inp = u.get("prompt_tokens") or 0
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            outt = u.get("completion_tokens") or 0
            started = ts(r.get("started"))
            # a background build can keep calling after the last turn ended: turn null, flagged
            after = bool(started and windows and windows[-1]["end"] and started > windows[-1]["end"])
            gcalls.append({"turn": None if after or not started else turn_at(started, windows),
                           "after_session": after, "purpose": r.get("purpose"),
                           "model": r.get("model"), "input_tokens": inp, "output_tokens": outt,
                           "cache_tokens": cached, "seconds": r.get("seconds"),
                           "usd": usd(inp, cached, outt, gat_price), "effort": r.get("effort"),
                           "started_at": r.get("started"), "error": r.get("error"), "file": os.path.basename(f)})
        write_jl(os.path.join(out, "gatherer_calls.jsonl"), gcalls)
    decisions.sort(key=lambda d: (d["turn"] or 0))
    write_jl(os.path.join(out, "decisions.jsonl"), decisions)

    # ---- turns.jsonl
    by_turn = defaultdict(list)
    for c in calls:
        by_turn[c["turn"]].append(c)
    g_by_turn = defaultdict(list)
    for g in gcalls:
        g_by_turn[g["turn"]].append(g)
    d_by_turn = defaultdict(list)
    for d in decisions:
        d_by_turn[d["turn"]].append(d)
    turns = []
    for r in tlog:
        t = r["turn"]
        cs = by_turn.get(t, [])
        agent = [c for c in cs if c["purpose"] == "agent"]
        gs = g_by_turn.get(t, [])
        ev = []
        if compaction_turns.get(t) or any(c["purpose"] == "compaction" for c in cs):
            ev.append("compaction")
        for d in d_by_turn.get(t, []):
            if d["kind"] == "boundary" and d["detail"]["new_task"]:
                ev.append("new_task")
            if d["kind"] == "build":
                ev.append("forced_build" if d["detail"]["forced"] else "build")
        if "error" in r.get("events", []) or r.get("error"):
            ev.append("error")
        turns.append({
            "turn": t, "visit": r["visit"], "client": r["client"], "thread": r["thread"],
            "started_at": r["started_at"], "seconds": r["seconds"], "actor_calls": len(agent),
            "actor_input_tokens": sum(c["input_tokens"] for c in cs),
            "actor_output_tokens": sum(c["output_tokens"] for c in cs),
            "actor_cache_tokens": sum(c["cache_tokens"] for c in cs),
            "context_tokens": max([c["input_tokens"] for c in agent] or [0]),
            "gatherer_input_tokens": sum(g["input_tokens"] for g in gs) if conn else None,
            "gatherer_output_tokens": sum(g["output_tokens"] for g in gs) if conn else None,
            "gatherer_seconds": round(sum(g["seconds"] or 0 for g in gs), 3) if conn else None,
            "actor_usd": round(sum(c["usd"] for c in cs), 6),
            "gatherer_usd": round(sum(g["usd"] for g in gs), 6) if conn else None,
            "events": ev, "error": r.get("error")})
    write_jl(os.path.join(out, "turns.jsonl"), turns)

    # ---- cost.json
    actor_usd = sum(c["usd"] for c in calls)
    gatherer_usd = sum(g["usd"] for g in gcalls)
    cost = {"actor_usd": round(actor_usd, 4), "gatherer_usd": round(gatherer_usd, 4) if conn else 0.0,
            "total_usd": round(actor_usd + gatherer_usd, 4),
            "gatherer_usd_after_session": round(sum(g["usd"] for g in gcalls if g.get("after_session")), 4),
            "actor_tokens": {"input": sum(c["input_tokens"] for c in calls),
                             "cached": sum(c["cache_tokens"] for c in calls),
                             "output": sum(c["output_tokens"] for c in calls), "calls": len(calls),
                             "compaction_calls": sum(c["purpose"] == "compaction" for c in calls)},
            "gatherer_tokens": {"input": sum(g["input_tokens"] for g in gcalls),
                                "cached": sum(g["cache_tokens"] for g in gcalls),
                                "output": sum(g["output_tokens"] for g in gcalls), "calls": len(gcalls),
                                "by_purpose": {p: sum(1 for g in gcalls if g["purpose"] == p)
                                               for p in sorted({g["purpose"] for g in gcalls})}} if conn else None,
            "prices": "run/prices.json", "item_token_counter": COUNTER}
    json.dump(cost, open(os.path.join(run_dir, "cost.json"), "w"), indent=1)
    write_jl(os.path.join(out, "messages.jsonl"), messages_rows(raw, tlog, calls))
    json.dump(tool_attempts(raw, run),open(os.path.join(run_dir, "tool_attempts.json"), "w"), indent=1)
    print(json.dumps({k: cost[k] for k in ("actor_usd", "gatherer_usd", "total_usd")}),
          f"turns={len(turns)} calls={len(calls)} decisions={len(decisions)} builds={len(builds_out)} "
          f"gatherer_calls={len(gcalls)}")


def messages_rows(raw, tlog, calls):
    """trace/messages.jsonl (TRACE.md): per user turn, the user text and the agent's steps, from the pi
    session file of the turn's thread. The k-th assistant message of a thread is matched to the k-th
    agent call of that thread (compaction calls are not in the session file)."""
    turns_by_thread = defaultdict(list)
    for r in sorted(tlog, key=lambda r: r["turn"]):
        turns_by_thread[r.get("thread") or "main"].append(r["turn"])
    calls_by_thread = defaultdict(list)
    for c in calls:
        if c["purpose"] == "agent":
            calls_by_thread[c.get("thread") or "main"].append(c["call_id"])
    rows = []
    for thread, tturns in turns_by_thread.items():
        files = sorted(glob.glob(os.path.join(raw, "pi", thread, "*.jsonl")))
        ui, ai, cur = 0, 0, None
        cids = calls_by_thread.get(thread, [])
        for f in files:
            for line in open(f):
                e = json.loads(line)
                m = e.get("message") if e.get("type") == "message" else None
                if not m:
                    continue
                content = m.get("content")
                parts = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
                if m["role"] == "user":
                    if cur:
                        rows.append(cur)
                    turn = tturns[ui] if ui < len(tturns) else None
                    ui += 1
                    cur = {"turn": turn, "thread": thread,
                           "user": "".join(p.get("text", "") for p in parts if p.get("type") == "text"), "steps": []}
                elif cur is None:
                    continue
                elif m["role"] == "assistant":
                    cid = cids[ai] if ai < len(cids) else None
                    ai += 1
                    for p in parts:
                        k = p.get("type")
                        if k == "text" and p.get("text", "").strip():
                            cur["steps"].append({"call_id": cid, "kind": "text", "name": None, "text": p["text"]})
                        elif k == "thinking" and (p.get("thinking") or p.get("text")):
                            cur["steps"].append({"call_id": cid, "kind": "thinking", "name": None,
                                                 "text": p.get("thinking") or p.get("text")})
                        elif k == "toolCall":
                            cur["steps"].append({"call_id": cid, "kind": "tool_call", "name": p.get("name"),
                                                 "text": json.dumps(p.get("arguments") or {}, ensure_ascii=False)})
                elif m["role"] == "toolResult":
                    txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
                    cur["steps"].append({"call_id": None, "kind": "tool_result", "name": m.get("toolName"),
                                         "text": txt[:2000]})
        if cur:
            rows.append(cur)
        if ai != len(cids):
            print(f"warning: thread {thread}: {ai} assistant messages vs {len(cids)} agent calls")
    rows.sort(key=lambda r: (r["turn"] is None, r["turn"] or 0))
    return rows


AG_CMD = re.compile(r"\bag\s+(?:actors|show|status|gatherers|stop|home|config|new|model|dashboard)\b")
SHELL = re.compile(r"```(?:bash|sh|shell|zsh)\b|\bsqlite3\b|\bcontext\.db\b|^\s*\$ \w+", re.M)


def tool_attempts(raw, run):
    """How often the agent reached for something it cannot run (no bash in any condition): calls to
    undeclared tools, tool errors, AG commands or shell snippets written in its replies, and read/ls
    of paths outside the workspace (e.g. AG's database). From pi's session files."""
    allowed = set((run.get("pi_tools") or "read,write,edit,ls").split(","))
    out = {"undeclared_tool_calls": 0, "tool_errors": 0, "ag_command_mentions": 0, "shell_mentions": 0,
           "outside_workspace_paths": 0, "examples": []}
    for f in sorted(glob.glob(os.path.join(raw, "pi", "*", "*.jsonl"))):
        for line in open(f):
            e = json.loads(line)
            m = e.get("message") if e.get("type") == "message" else None
            if not m or not isinstance(m.get("content"), list):
                continue
            if m["role"] == "toolResult" and m.get("isError"):
                out["tool_errors"] += 1
                txt = "".join(p.get("text", "") for p in m["content"])
                out["examples"].append({"kind": "tool_error", "tool": m.get("toolName"), "text": txt[:200]})
            if m["role"] != "assistant":
                continue
            for p in m["content"]:
                if p.get("type") == "toolCall":
                    args = p.get("arguments") or {}
                    if p.get("name") not in allowed:
                        out["undeclared_tool_calls"] += 1
                        out["examples"].append({"kind": "undeclared", "tool": p.get("name"), "args": str(args)[:200]})
                    path = str(args.get("path", "")) if p.get("name") in ("read", "ls") else ""
                    if path.startswith(("/", "~")) or ".." in path or ".ag" in path:
                        out["outside_workspace_paths"] += 1
                        out["examples"].append({"kind": "outside_path", "tool": p.get("name"), "path": path[:200]})
                elif p.get("type") == "text":
                    t = p.get("text", "")
                    for k, rx in (("ag_command_mentions", AG_CMD), ("shell_mentions", SHELL)):
                        hits = rx.findall(t)
                        if hits:
                            out[k] += len(hits)
                            out["examples"].append({"kind": k, "text": hits[0][:100]})
    out["examples"] = out["examples"][:20]
    out["total"] = sum(out[k] for k in ("undeclared_tool_calls", "tool_errors", "ag_command_mentions",
                                         "shell_mentions", "outside_workspace_paths"))
    return out


if __name__ == "__main__":
    main()
