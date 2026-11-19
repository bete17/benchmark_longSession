"""Step 3: does AG's boundary check (and segmentation) cut the session where visits.json intends?

Feeds the scripted user messages, with short placeholder agent replies, through AG's own tools at
59ca484 (ag_policies/tools/boundary.py and segmentation.py) with the Gatherer model, replaying the
policy's decision loop (proactive_context_manager/policy.py: start_due, check, build_within) under the
ag_half configuration:

  at each user message, the boundary check runs only if
    - no build is running            (idealized here: every build finishes before the next message;
                                       in a live run a build takes ~100 s and blocks checks meanwhile)
    - the context is >= min_context_tokens   (estimated; see CONTEXT_BASE)
    - more than boundary_cooldown user messages are in the open task
  on a yes, a build segments the history (segmentation.update, as build_within does); with >= 2 tasks
  it is adopted and the open task starts at the last task's first turn; with < 2 it is "nothing" and
  the cooldown counts from that message.

Ranking and summaries are skipped: neither changes where tasks are cut (the boundary prompt shows the
previous tasks' names and descriptions, which segmentation writes).

For every visit switch the report says: checked or never checked (and which gate), and if checked,
whether it said new task. For switches that were never checked it also asks the check once,
off the record ("would_say"), so a gate problem can be told apart from a judgement problem. Those
off-record calls do not change the state.

  AG venv python:  ../ag-bench/venv/bin/python run/granularity.py --session sessions/pilot_seed0 \
                       --out results/granularity [--max-usd 4.5] [--max-turns N]

Writes <out>/switches.jsonl, <out>/turns.jsonl, <out>/tasks.json, <out>/report.json and prints a
summary. AG's call log (prompts included) goes to <out>/raw/llm/ (gitignored).
"""

import argparse
import dataclasses
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_session import AG_SCALED_100K, MODEL  # noqa: E402

from ag.ag_policies.proactive_context_manager.config import ProactiveContextManagerConfig  # noqa: E402
from ag.ag_policies.tools import boundary, segmentation  # noqa: E402
from ag.ag_policies.tools.groups import AGENT_MESSAGE, TOOL_CALL, USER_MESSAGE  # noqa: E402
from ag.ag_policies.tools.history import Entry, Task, Turn  # noqa: E402
from ag.ag_policies.tools.tasks import locate  # noqa: E402
from ag.ag_tools.llm.litellm_client import LiteLLMClient  # noqa: E402
from ag.ag_tools.tokenizers.tiktoken_tokenizer import Tiktoken  # noqa: E402

GATHERER = f"gemini/{MODEL}"   # default; --model overrides (e.g. during an API outage)
# What the actor's context holds besides the open task under ag pi, measured in the smoke run
# (ag_default_seed0_rep1, turn 1: 10,543 Gemini tokens with a 1-turn history). Only used for the
# min_context_tokens gate; with min_context_tokens = 7500 that gate always passes.
CONTEXT_BASE = 9000
GEMINI_PER_TIKTOKEN = 1.35      # Gemini counts this session's text ~1.35x tiktoken (RUNLOG step 2)


class CappedLLM(LiteLLMClient):
    """AG's LiteLLM client with a spend cap read from its own call log."""

    def __init__(self, log_dir, price, max_usd):
        super().__init__(log_dir=log_dir, log_prompts=True)
        self.dir, self.price, self.max_usd = log_dir, price, max_usd

    def spent(self):
        total = 0.0
        for f in glob.glob(os.path.join(self.dir, "*.json")):
            try:
                u = json.load(open(f)).get("usage") or {}
            except ValueError:
                continue
            inp = u.get("prompt_tokens") or 0
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            out = u.get("completion_tokens") or 0
            total += ((inp - cached) * self.price["input"] + cached * self.price["cache_read"]
                      + out * self.price["output"]) / 1e6
        return total

    retries = 0
    failures = []

    def complete(self, prompt, **kw):
        """AG's call, retried on an API error (503, timeout) so a provider hiccup is not measured as a
        verdict: in AG a failed boundary call reads as "same task". Gives up after 6 tries."""
        for attempt, wait in enumerate((0, 15, 30, 60, 120, 240, 300, 600, 600, 900)):
            s = self.spent()
            if s >= self.max_usd:
                raise SystemExit(f"spend cap ${self.max_usd} reached (${s:.2f}); stopping")
            time.sleep(wait)
            text = super().complete(prompt, **kw)
            logs = sorted(glob.glob(os.path.join(self.dir, "*.json")))
            err = json.load(open(logs[-1])).get("error") if logs else None
            if not err:
                return text
            CappedLLM.retries += 1
            print(f"  {kw.get('purpose')} call failed (try {attempt + 1}): {str(err)[:120]}", flush=True)
        CappedLLM.failures.append({"purpose": kw.get("purpose"), "error": str(err)[:300]})
        return ""


def make_turns(session, tok):
    """AG Turns from the scripted messages: the user's text, a short placeholder reply, and a tool
    call where the message asks for a deliverable file (as render_turn would see a real run)."""
    turns, row = [], 0
    for m in session:
        row += 1
        entries = [Entry(row, USER_MESSAGE, m["text"], None, tok.count(m["text"]), is_user=True, is_message=True)]
        d = re.search(r"deliverables/(D\d+)\.md", m["text"])
        if d:
            row += 1
            entries.append(Entry(row, TOOL_CALL, f"write deliverables/{d.group(1)}.md", None, 8, is_tool=True))
            reply = f"Done, I wrote deliverables/{d.group(1)}.md."
        else:
            reply = "Got it, noted."
        row += 1
        entries.append(Entry(row, AGENT_MESSAGE, reply, None, tok.count(reply), is_message=True))
        turns.append(Turn(m["turn"], entries))
    return turns


def task_row(t):
    return {"name": t.name, "start_turn": t.start_turn, "end_turn": t.end_turn, "tokens": t.tokens,
            "description": t.description}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-usd", type=float, default=4.5)
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--model", default=None, help="gatherer model, e.g. gemini/gemini-3-flash-preview")
    a = ap.parse_args()
    global GATHERER
    if a.model:
        GATHERER = a.model

    prices = json.load(open(os.path.join(HERE, "prices.json")))
    # spend cap priced at gemini-3.8-flash rates (an upper bound for older Flash models)
    price = prices["models"][MODEL]
    cfg = dataclasses.replace(ProactiveContextManagerConfig(), **AG_SCALED_100K,
                              boundary_model=GATHERER, segment_model=GATHERER)
    bparams, sparams = cfg.boundary, cfg.segmentation
    os.makedirs(os.path.join(a.out, "raw", "llm"), exist_ok=True)
    llm = CappedLLM(os.path.join(a.out, "raw", "llm"), price, a.max_usd)
    llm.model = GATHERER
    tok = Tiktoken()

    session = [json.loads(l) for l in open(os.path.join(a.session, "session.jsonl"))]
    visits = json.load(open(os.path.join(a.session, "visits.json")))
    if a.max_turns:
        session = session[:a.max_turns]
    all_turns = make_turns(session, tok)
    switch_at = {v["first_turn"]: v for v in visits if v["first_turn"] > 1}
    prev_visit = {v["first_turn"]: visits[i - 1] for i, v in enumerate(visits) if i > 0}

    stored = []                 # tasks AG keeps (tables.save_tasks)
    open_from = None            # first turn of the open task of the adopted build (in_use)
    since = 0                   # after a "nothing" build: count user messages after this turn
    rows_out, switches = [], []
    t0 = time.time()

    def call_check(turns_so_far, stored_now):
        located = locate([dataclasses.replace(t) for t in stored_now], turns_so_far)
        start = located[-1].start_turn if located else turns_so_far[0].index
        open_turns = [t for t in turns_so_far if t.index >= start]
        if len(open_turns) <= 1:
            return False, "first turn of the open task", len(open_turns)
        nfail = len(CappedLLM.failures)
        v, why = boundary.is_new_task(open_turns, located[:-1], turns_so_far, llm, bparams)
        if len(CappedLLM.failures) > nfail:
            return None, "API error after all retries (not a verdict)", len(open_turns)
        if v is None:
            v, why = False, f"{why}; treated as a continuation"
        return v, why, len(open_turns)

    try:
        for i, turn in enumerate(all_turns):
            t = turn.index
            so_far = all_turns[:i + 1]
            # open task as the context sees it (verbatim part), for the size gate
            vstart = open_from if open_from else 1
            open_tok = sum(x.message_tokens for x in so_far if x.index >= vstart)
            size = CONTEXT_BASE + int(open_tok * GEMINI_PER_TIKTOKEN)
            if open_from:
                into = t - open_from + 1
            else:
                into = t - since
            row = {"turn": t, "visit": session[i]["visit"], "client": session[i]["client"],
                   "est_context": size, "into_task": into, "checked": False, "gate": None,
                   "new_task": None, "why": None, "open_turns": None, "build": None}
            if size < cfg.min_context_tokens:
                row["gate"] = "min_context"
            elif into <= cfg.boundary_cooldown:
                row["gate"] = "cooldown"
            else:
                v, why, n = call_check(so_far, stored)
                row.update(checked=True, new_task=v, why=why, open_turns=n)
                if v:
                    tasks, failed, _fresh = segmentation.update(so_far, stored, llm, sparams, tok)
                    if failed:
                        row["build"] = {"status": "failed"}
                        since, open_from = t, None
                    else:
                        stored = tasks
                        cuts = sorted(x.start_turn for x in locate([dataclasses.replace(x) for x in tasks], so_far)
                                      if x.start_turn > 1)
                        if len(tasks) < 2:
                            row["build"] = {"status": "nothing", "tasks": len(tasks), "cuts": cuts}
                            since, open_from = t, None
                        else:
                            open_from = tasks[-1].start_turn
                            row["build"] = {"status": "ready", "tasks": len(tasks), "open_from": open_from,
                                            "cuts": cuts}
            rows_out.append(row)

            if t in switch_at:
                v, pv = switch_at[t], prev_visit[t]
                sw = {"turn": t, "visit": v["visit"], "from_client": pv["client"], "to_client": v["client"],
                      "from_kind": pv["kind"], "to_kind": v["kind"], "client_change": pv["client"] != v["client"],
                      "checked": row["checked"], "gate": row["gate"], "new_task": row["new_task"],
                      "why": row["why"], "would_say": None, "would_why": None}
                if not row["checked"]:
                    wv, wwhy, _ = call_check(so_far, stored)     # off the record: state unchanged
                    sw.update(would_say=wv, would_why=wwhy)
                sw["outcome"] = ("api_error" if row["checked"] and row["new_task"] is None else
                                 "detected" if row["new_task"] else
                                 "checked_missed" if row["checked"] else "never_checked")
                switches.append(sw)
            print(f"turn {t:3d} into={into:2d} gate={row['gate'] or '-':11s} "
                  f"new={row['new_task']!s:5s} {'SWITCH ' + switches[-1]['outcome'] if t in switch_at else ''} "
                  f"${llm.spent():.3f}", flush=True)
    except SystemExit as e:
        print(str(e))

    # final segmentation of everything seen, as one more build would make it
    try:
        final, failed, _ = segmentation.update(all_turns[:len(rows_out)], stored, llm, sparams, tok)
    except SystemExit as e:
        print(str(e))
        final, failed = stored, True
    final = locate(final, all_turns[:len(rows_out)])

    # ---- report
    last_turn = rows_out[-1]["turn"] if rows_out else 0
    sws = [s for s in switches if s["turn"] <= last_turn]
    # for each switch the boundary check did not catch: the next build after it, and whether that build's
    # segmentation cut at the switch (exactly, or within one turn)
    builds = [(r["turn"], r["build"].get("cuts") or []) for r in rows_out if r.get("build") and "cuts" in r["build"]]
    for s in sws:
        nb = next(((bt, cuts) for bt, cuts in builds if bt > s["turn"]), None)
        s["next_build_turn"] = nb[0] if nb else None
        s["next_build_cut"] = (s["turn"] in nb[1]) if nb else None
        s["next_build_cut_pm1"] = any(abs(c - s["turn"]) <= 1 for c in nb[1]) if nb else None

    def rate(xs, key="outcome", val="detected"):
        return round(sum(x[key] == val for x in xs) / len(xs), 3) if xs else None

    intended = {s["turn"] for s in sws}
    yes_turns = {r["turn"] for r in rows_out if r["new_task"]}
    cut_turns = {t.start_turn for t in final if t.start_turn > 1}
    near = lambda s, ref: any(abs(x - y) <= 1 for y in ref for x in [s])  # noqa: E731

    def pr(found, ref):
        if not found or not ref:
            return {"precision": None, "recall": None, "n_found": len(found), "n_intended": len(ref)}
        return {"precision": round(len(found & ref) / len(found), 3), "recall": round(len(found & ref) / len(ref), 3),
                "precision_pm1": round(sum(near(f, ref) for f in found) / len(found), 3),
                "recall_pm1": round(sum(near(r, found) for r in ref) / len(ref), 3),
                "n_found": len(found), "n_intended": len(ref)}

    merged = []
    for v in visits:
        if v["first_turn"] > last_turn or v["first_turn"] == 1:
            continue
        if v["first_turn"] not in cut_turns:
            merged.append({"visit": v["visit"], "first_turn": v["first_turn"], "client": v["client"],
                           "kind": v["kind"], "into_task": next((t.name for t in final
                                                                 if t.start_turn <= v["first_turn"] <= t.end_turn), None)})
    report = {
        "session": a.session, "turns": last_turn, "gatherer_model": GATHERER, "ag_commit": "59ca484",
        "config": "ag_half", "min_context_tokens": cfg.min_context_tokens, "boundary_cooldown": cfg.boundary_cooldown,
        "idealized": "every build finishes before the next user message; ranking and summaries skipped",
        "switches": len(sws),
        "detected": sum(s["outcome"] == "detected" for s in sws),
        "checked_missed": sum(s["outcome"] == "checked_missed" for s in sws),
        "api_error": sum(s["outcome"] == "api_error" for s in sws),
        "never_checked": sum(s["outcome"] == "never_checked" for s in sws),
        "missed_but_cut_at_next_build": sum(1 for s in sws if s["outcome"] != "detected" and s.get("next_build_cut")),
        "missed_no_later_build": sum(1 for s in sws if s["outcome"] != "detected" and s.get("next_build_turn") is None),
        "never_checked_by_gate": {g: sum(1 for s in sws if s["gate"] == g) for g in ("cooldown", "min_context")},
        "never_checked_would_say_new": sum(1 for s in sws if s["outcome"] == "never_checked" and s["would_say"]),
        "detection_rate": rate(sws),
        "detection_rate_client_change": rate([s for s in sws if s["client_change"]]),
        "detection_rate_same_client": rate([s for s in sws if not s["client_change"]]),
        "checks_run": sum(r["checked"] for r in rows_out),
        "false_new_task": sorted(yes_turns - intended),
        "boundary_check_vs_visits": pr(yes_turns, intended),
        "final_segmentation_vs_visits": pr(cut_turns, intended),
        "final_tasks": len(final), "segmentation_failed": failed,
        "merged_visits": merged,
        "gatherer_usd": round(llm.spent(), 4), "seconds": round(time.time() - t0, 1),
        "calls_by_purpose": {}, "api_retries": CappedLLM.retries, "api_failures_after_retries": CappedLLM.failures,
    }
    for f in glob.glob(os.path.join(a.out, "raw", "llm", "*.json")):
        p = json.load(open(f)).get("purpose")
        report["calls_by_purpose"][p] = report["calls_by_purpose"].get(p, 0) + 1

    with open(os.path.join(a.out, "switches.jsonl"), "w") as f:
        f.writelines(json.dumps(s) + "\n" for s in sws)
    with open(os.path.join(a.out, "turns.jsonl"), "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows_out)
    json.dump([task_row(t) for t in final], open(os.path.join(a.out, "tasks.json"), "w"), indent=1)
    json.dump(report, open(os.path.join(a.out, "report.json"), "w"), indent=1)
    print(json.dumps({k: v for k, v in report.items() if k not in ("merged_visits",)}, indent=1))


if __name__ == "__main__":
    main()
