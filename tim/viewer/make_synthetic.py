"""Write fake runs in the TRACE.md layout so the viewer and attribution can be tested before real runs
exist. Numbers are invented; the structure is the real one. Marked "synthetic": true in run.json.

  python viewer/make_synthetic.py --session sessions/pilot_seed0 --out results/_synthetic

Writes four runs: pi_compact rep1/rep2, ag_default rep1, ag_tight rep1. Deliverables are produced by a
simple retrieval model (a fact reaches the model with some probability that depends on the condition
and the fact's age); grade/grade.py then grades them, so grades.jsonl is real grader output.
"""

import argparse
import json
import os
import random
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TIM = os.path.dirname(HERE)
CEILING = 100_000
TOK_PER_TURN = 1500          # user text + agent output + tool results, per user turn (invented)
START = 1_800_000_000.0


def load(session):
    sess = [json.loads(l) for l in open(os.path.join(session, "session.jsonl"))]
    checks = [json.loads(l) for l in open(os.path.join(session, "checks.jsonl"))]
    facts = json.load(open(os.path.join(session, "facts.json")))
    visits = json.load(open(os.path.join(session, "visits.json")))
    return sess, checks, facts, visits


def fact_source(facts, client, field, stated_turn):
    for f in facts[client][field]:
        if f["turn"] == stated_turn:
            return f
    return None


def jl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def make_run(root, session, cond, rep, seed=0):
    rng = random.Random(hash((cond, rep, seed)) & 0xFFFFFFFF)
    sess, checks, facts, visits = load(session)
    n_turns = len(sess)
    run_id = f"{cond}_seed{seed}_rep{rep}"
    d = os.path.join(root, run_id)
    for sub in ("deliverables", "trace", "raw/requests"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    is_ag = cond.startswith("ag")
    per_client = cond == "pi_per_client"
    visit_of = {}
    for v in visits:
        for t in range(v["first_turn"], v["last_turn"] + 1):
            visit_of[t] = v
    turns, calls, contexts, decisions, builds, gcalls = [], [], [], [], [], []
    ctx_tokens = 0
    last_compact_turn = 0
    summary_facts = set()            # (client, field, turn) retained by the compaction summary
    clock = START
    checks_by_turn = defaultdict(list)
    for c in checks:
        checks_by_turn[c["turn"]].append(c)
    tasks, build_id, task_start = [], 0, 1
    cost_a = cost_g = 0.0
    seen_user = 0
    p_ag = 0.9 if cond == "ag_default" else 0.8      # chance AG pages in a needed old fact
    deliver = {}                                      # deliverable_id -> (header, body)
    for t in range(1, n_turns + 1):
        v = visit_of[t]
        client = v["client"]
        n_calls = 1 + (1 if t in checks_by_turn else 0) + rng.randint(0, 1)
        events, g_in, g_out, g_sec, g_usd = [], 0, 0, 0.0, 0.0
        seen_user += 1
        thread = client if per_client else "main"
        # --- context manager step
        if per_client:
            pass
        if is_ag:
            first_of_visit = t == v["first_turn"] and v["visit"] > 0
            if ctx_tokens > 15_000 and t - task_start >= 2:
                new_task = first_of_visit and rng.random() < 0.85
                decisions.append({"turn": t, "kind": "boundary", "detail": {
                    "new_task": int(new_task),
                    "why": ("topic moved to " + client) if new_task else "same task, follow-up on the same account"}})
                gg = (2200, 60, 1.1, 0.0009)
                gcalls.append({"turn": t, "purpose": "boundary", "model": "gemini-flash", "input_tokens": gg[0],
                               "output_tokens": gg[1], "seconds": gg[2], "usd": gg[3]})
                g_in += gg[0]; g_out += gg[1]; g_sec += gg[2]; g_usd += gg[3]
                if new_task:
                    events.append("new_task")
                    task_start = t
                    build_id += 1
                    seg = (14_000 + 300 * build_id, 900, 5.5 + rng.random() * 3, 0.006 + 0.0002 * build_id)
                    for purpose, scale in (("segment", 1.0), ("rank", 0.4), ("summarize", 0.6)):
                        gcalls.append({"turn": t, "purpose": purpose, "model": "gemini-flash",
                                       "input_tokens": int(seg[0] * scale), "output_tokens": int(seg[1] * scale),
                                       "seconds": round(seg[2] * scale, 2), "usd": round(seg[3] * scale, 5)})
                        g_in += int(seg[0] * scale); g_out += int(seg[1] * scale)
                        g_sec += seg[2] * scale; g_usd += seg[3] * scale
                    events.append("build")
                    prior = []
                    for vv in visits:
                        if vv["last_turn"] < t:
                            prior.append(vv)
                    tk = [{"index": i, "name": f"{vv['client']} {vv['kind']}", "description": f"visit {vv['visit']}",
                           "first_turn": vv["first_turn"], "last_turn": vv["last_turn"],
                           "tokens": (vv["last_turn"] - vv["first_turn"] + 1) * TOK_PER_TURN,
                           "retention": "established " + ", ".join(p[1] for p in vv.get("planted", [])[:3])}
                          for i, vv in enumerate(prior)]
                    rank = [vv["visit"] for vv in sorted(prior, key=lambda x: (x["client"] != client, -x["last_turn"]))][:8]
                    builds.append({"build_id": build_id, "turn": t, "status": "ok", "seconds": round(seg[2], 2),
                                   "open_from_turn": t, "tasks": tk, "ranking": rank,
                                   "sections": [{"name": f"{client} facts", "tokens": 2400},
                                                {"name": "previous task", "tokens": 7500 if cond == "ag_tight" else 15000},
                                                {"name": "pinned", "tokens": 3000}]})
                    decisions.append({"turn": t, "kind": "build", "detail": {
                        "build_id": build_id, "status": "ok", "seconds": round(seg[2], 2), "through_turn": t - 1,
                        "open_from_turn": t, "forced": False}})
            ctx_tokens = min(ctx_tokens + TOK_PER_TURN, 45_000 if cond == "ag_tight" else 62_000)
        else:
            ctx_tokens += TOK_PER_TURN
            if ctx_tokens > CEILING - 16_384:
                before = ctx_tokens
                ctx_tokens = 11_000
                events.append("compaction")
                # which earlier facts survive the summary: home 0.6, embedded 0.3, newer more likely
                for cl, fields in facts.items():
                    for fld, vals in fields.items():
                        for fv in vals:
                            if fv["turn"] <= t - 1:
                                p = (0.6 if fv["placement"] == "home" else 0.3) + (0.2 if fv["turn"] > t - 30 else 0)
                                if rng.random() < p:
                                    summary_facts.add((cl, fld, fv["turn"]))
                last_compact_turn = t
                decisions.append({"turn": t, "kind": "compaction", "detail": {
                    "thread": thread, "tokens_before": before, "tokens_after": ctx_tokens,
                    "summary_chars": 9000 + rng.randint(-1500, 1500)}})
                g_in = 0
        # --- calls on this turn
        turn_in = turn_out = turn_cache = 0
        t0 = clock
        for k in range(n_calls):
            cid = f"t{t:03d}c{k}"
            inp = ctx_tokens + k * 600
            out = 250 + rng.randint(0, 500)
            cache = int(inp * (0.7 if k else 0.4))
            sec = 2.0 + inp / 40_000 + rng.random() * 2
            writes = t in checks_by_turn and k == n_calls - 1
            calls.append({"call_id": cid, "turn": t, "thread": thread, "index_in_turn": k,
                          "started_at": clock, "seconds": round(sec, 2), "input_tokens": inp,
                          "output_tokens": out, "cache_tokens": cache,
                          "stop_reason": "tool_use" if writes else "end_turn",
                          "tool_calls": ["write"] if writes else [], "request_file": f"raw/requests/{cid}.json"})
            clock += sec
            turn_in += inp; turn_out += out; turn_cache += cache
            # --- what is in front of the model on this call
            items = [{"kind": "system", "origin_turn": None, "role": "system", "tokens": 450, "label": "system prompt"}]
            req_msgs = []
            if is_ag:
                items.append({"kind": "summary", "origin_turn": None, "role": "user", "tokens": 3000, "label": "pinned"})
                items.append({"kind": "summary", "origin_turn": None, "role": "user", "tokens": 2400, "label": f"{client} facts"})
                window_start = max(1, t - 14 if cond == "ag_default" else t - 10)
            else:
                if last_compact_turn:
                    items.append({"kind": "summary", "origin_turn": None, "role": "user", "tokens": 4200,
                                  "label": "compaction summary"})
                window_start = max(1, last_compact_turn or 1)
                if not last_compact_turn:
                    window_start = 1
            shown = list(range(window_start, t + 1))
            if len(shown) > 40:
                shown = shown[-40:]          # keep the synthetic request files small
            for tt in shown:
                items.append({"kind": "message", "origin_turn": tt, "role": "user",
                              "tokens": TOK_PER_TURN // 2, "label": ""})
                req_msgs.append({"role": "user", "content": sess[tt - 1]["text"]})
            if writes:
                for c in checks_by_turn[t]:
                    if c["kind"] != "field" and c["kind"] != "style":
                        continue
                    st = c["stated_turn"]
                    if st is None or st in shown:
                        continue
                    f = fact_source(facts, c["client"], c["field"], st) if c["kind"] == "field" else None
                    src = f["sentence"] if f else f"{c['client']} prefers the word '{c['expected']['include']}'."
                    if is_ag:
                        got = rng.random() < (p_ag if c["placement"] == "home" else p_ag - 0.35)
                        if got:
                            items.append({"kind": "message", "origin_turn": st, "role": "user",
                                          "tokens": TOK_PER_TURN // 2, "label": "paged in"})
                            req_msgs.append({"role": "user", "content": sess[st - 1]["text"]})
                    else:
                        key = (c["client"], c["field"], st)
                        if key in summary_facts or (c["field"] == "style_rule" and rng.random() < 0.4):
                            txt = (f"{c['client']}: {c['field']} = {c['expected']}" if c["kind"] == "field"
                                   else f"{c['client']} preferred wording noted ({c['expected']['include']}).")
                            req_msgs.append({"role": "user", "content": "[compaction summary] " + txt})
            contexts.append({"call_id": cid, "turn": t, "total_tokens": inp, "items": items})
            with open(os.path.join(d, "raw/requests", cid + ".json"), "w") as f:
                json.dump({"system": "You are an assistant to an account manager.", "messages": req_msgs}, f)
        # --- deliverable for this turn, if any
        for did in sorted({c["deliverable_id"] for c in checks_by_turn.get(t, [])}):
            cs = [c for c in checks if c["deliverable_id"] == did]
            hdr = {"deliverable_id": did, "client": cs[0]["client"], "type": cs[0]["type"]}
            body = "Hello,\n\nThanks for the quarter."
            req_txt = json.dumps(json.load(open(os.path.join(d, "raw/requests", f"t{t:03d}c{n_calls-1}.json"))))
            for c in cs:
                if c["kind"] == "unknown":
                    hdr[c["field"]] = None if rng.random() < 0.85 else "N/A-guess"
                    if hdr[c["field"]] == "N/A-guess":
                        hdr[c["field"]] = 100
                    continue
                present = c["stated_turn"] is None
                if c["kind"] == "field":
                    present = str(c["expected"]) in req_txt
                else:
                    present = c["expected"]["include"] in req_txt
                if present and rng.random() > 0.07:
                    val = c["expected"]
                elif c["superseded_values"] and rng.random() < 0.6:
                    val = c["superseded_values"][0]
                else:
                    val = "unknown value" if c["kind"] == "field" else None
                if c["kind"] == "style":
                    body += f" Let's agree a {c['expected']['include'] if val else c['expected']['exclude']}."
                else:
                    hdr[c["field"]] = val
            with open(os.path.join(d, "deliverables", did + ".md"), "w") as f:
                f.write("```json\n" + json.dumps(hdr) + "\n```\n\n" + body + "\n")
        a_usd = turn_in * 0.3e-6 + turn_out * 2.5e-6 - turn_cache * 0.225e-6
        cost_a += a_usd
        cost_g += g_usd
        turns.append({"turn": t, "visit": v["visit"], "client": client, "thread": thread,
                      "started_at": t0, "seconds": round(clock - t0 + g_sec, 2), "actor_calls": n_calls,
                      "actor_input_tokens": turn_in, "actor_output_tokens": turn_out,
                      "actor_cache_tokens": turn_cache, "context_tokens": ctx_tokens + (n_calls - 1) * 600,
                      "gatherer_input_tokens": g_in, "gatherer_output_tokens": g_out,
                      "gatherer_seconds": round(g_sec, 2), "actor_usd": round(a_usd, 5),
                      "gatherer_usd": round(g_usd, 5), "events": events})
    jl(os.path.join(d, "trace/turns.jsonl"), turns)
    jl(os.path.join(d, "trace/calls.jsonl"), calls)
    jl(os.path.join(d, "trace/contexts.jsonl"), contexts)
    jl(os.path.join(d, "trace/decisions.jsonl"), decisions)
    if is_ag:
        jl(os.path.join(d, "trace/builds.jsonl"), builds)
        jl(os.path.join(d, "trace/gatherer_calls.jsonl"), gcalls)
    json.dump({"actor_usd": round(cost_a, 4), "gatherer_usd": round(cost_g, 4),
               "total_usd": round(cost_a + cost_g, 4)}, open(os.path.join(d, "cost.json"), "w"), indent=1)
    run = {"run_id": run_id, "condition": cond, "seed": seed, "rep": rep, "session_dir": session,
           "actor_model": "gemini-flash (synthetic)", "gatherer_model": "gemini-flash (synthetic)" if is_ag else None,
           "ag_commit": "59ca484" if is_ag else None, "pi_version": "synthetic", "ceiling_tokens": CEILING,
           "system_prompt_sha": "synthetic", "started_at": START, "finished_at": clock,
           "status": "complete", "turns_completed": n_turns, "errors": [], "synthetic": True}
    if cond == "ag_tight":
        run["ag_options"] = {"keep_task_relevant_tokens": 7500, "keep_temporal_relevant_tokens": 7500,
                             "keep_previous_task_tokens": 7500}
    json.dump(run, open(os.path.join(d, "run.json"), "w"), indent=1)
    subprocess.run([sys.executable, os.path.join(TIM, "grade", "grade.py"), "--session", session, "--run", d,
                    "--label", run_id], check=True, stdout=subprocess.DEVNULL)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=os.path.join(TIM, "sessions", "pilot_seed0"))
    ap.add_argument("--out", default=os.path.join(TIM, "results", "_synthetic"))
    a = ap.parse_args()
    for cond, rep in (("pi_compact", 1), ("pi_compact", 2), ("ag_default", 1), ("ag_tight", 1)):
        print(make_run(a.out, a.session, cond, rep))


if __name__ == "__main__":
    main()
