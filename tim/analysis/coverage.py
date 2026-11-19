"""Fact coverage: on every turn, what share of the facts said so far is still in front of the model?

This is the direct test of whether degradation is even possible. If compaction keeps every fact,
coverage stays near 100% and the old-fact checks cannot fail for retrieval reasons. If it drops after
each compaction (or AG fails to page facts back in), you see it here before reading any grade.

  python analysis/coverage.py --session sessions/pilot_seed0 --all results
  python analysis/coverage.py --session sessions/pilot_seed0 --run results/<run_id>

Reads trace/calls.jsonl and raw/requests/ (local only). Writes <run>/coverage.jsonl (committed; small):
one row per turn, using the turn's last agent call:
  turn, call_id, n, verbatim, paraphrased, absent, coverage (= (verbatim+paraphrased)/n),
  lost: ["Client.field", ...] facts said earlier whose current value is not in view.
"Facts said so far" = for each client and field, the latest value stated before this turn (so a
superseded value is not counted; the current one is). Style rules count as present if the preferred
word and the client name appear together.
Prints the coverage just before and just after each compaction (pi) or build (AG).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attribute import classify, load_jsonl, messages_text  # noqa: E402

try:  # Claude Code added a Gemini-body reader to attribute.py; use it when present
    from attribute import request_messages as _req_msgs  # type: ignore
except Exception:  # pragma: no cover
    _req_msgs = None


def req_texts(path):
    req = json.load(open(path))
    return _req_msgs(req) if _req_msgs else messages_text(req)


def current_facts(facts, turn):
    """{(client, field): fact} with the latest value stated strictly before `turn`"""
    out = {}
    for client, fields in facts.items():
        for field, vals in fields.items():
            said = [v for v in vals if v["turn"] < turn]
            if said:
                out[(client, field)] = max(said, key=lambda v: v["turn"])
    return out


def as_check(client, field, fv):
    if field == "style_rule":
        banned, preferred = fv["value"]
        return {"client": client, "kind": "style", "expected": {"include": preferred, "exclude": banned}}
    return {"client": client, "kind": "field", "expected": fv["value"]}


def run_coverage(run, facts):
    calls = [c for c in load_jsonl(os.path.join(run, "trace", "calls.jsonl")) if c.get("purpose", "agent") == "agent"]
    last = {}
    for c in calls:
        last[c["turn"]] = c
    rows = []
    for turn in sorted(last):
        c = last[turn]
        rf = os.path.join(run, c["request_file"])
        if not os.path.exists(rf):
            continue
        texts = req_texts(rf)
        cur = current_facts(facts, turn + 1)   # include facts said on this very turn
        tally = {"verbatim": 0, "paraphrased": 0, "absent": 0}
        lost = []
        for (client, field), fv in sorted(cur.items()):
            p = classify(as_check(client, field, fv), texts, fv["sentence"])
            tally[p] += 1
            if p == "absent":
                lost.append(f"{client}.{field}")
        n = sum(tally.values())
        rows.append({"turn": turn, "call_id": c["call_id"], "n": n, **tally,
                     "coverage": round((tally["verbatim"] + tally["paraphrased"]) / n, 4) if n else None,
                     "lost": lost})
    if rows:
        with open(os.path.join(run, "coverage.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return rows


def report(run, rows):
    dec = load_jsonl(os.path.join(run, "trace", "decisions.jsonl"))
    events = sorted({d["turn"] for d in dec if d["kind"] in ("compaction", "build")})
    by = {r["turn"]: r for r in rows}
    print(os.path.basename(os.path.normpath(run)), f"turns with requests: {len(rows)}")
    if not rows:
        print("  no raw/requests here (local only)")
        return
    print(f"  coverage at end: {rows[-1]['coverage']:.0%} of {rows[-1]['n']} facts; "
          f"mean {sum(r['coverage'] for r in rows if r['coverage'] is not None)/len(rows):.0%}")
    for t in events:
        b, a = by.get(t - 1), by.get(t)
        if b and a:
            print(f"  turn {t}: {b['coverage']:.0%} before, {a['coverage']:.0%} after; newly lost: "
                  + ", ".join(sorted(set(a['lost']) - set(b['lost']))[:8]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--run")
    ap.add_argument("--all")
    a = ap.parse_args()
    facts = json.load(open(os.path.join(a.session, "facts.json")))
    runs = [a.run] if a.run else []
    if a.all:
        for dp, dn, fn in os.walk(a.all):
            if "run.json" in fn:
                runs.append(dp)
                dn[:] = []
    for r in sorted(runs):
        report(r, run_coverage(r, facts))


if __name__ == "__main__":
    main()
