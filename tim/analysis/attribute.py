"""Attribution: for every graded check, was the fact in front of the model when it wrote the deliverable?

  python analysis/attribute.py --session sessions/pilot_seed0 --run results/<run_id>
  python analysis/attribute.py --session sessions/pilot_seed0 --all results     (every run under a folder)

Reads  <run>/grades.jsonl, <run>/trace/calls.jsonl, <run>/raw/requests/<call_id>.json, session facts.json.
Writes <run>/attribution.jsonl and prints a table. See TRACE.md for the definitions.

Writing call for a check = the last call in the check's turn whose tool_calls include a write tool
(write/edit/create), else the last call of the turn.

presence:
  verbatim      the fact's exact source sentence (facts.json) is in the request text
                (for style checks: the style sentence recorded in checks, matched on the preferred word + client)
  paraphrased   the expected value and the client name both appear inside ONE message/item of the request
                (for example in a compaction summary or an AG-made item)
  absent        neither
Shared (G) checks (scope "shared", source sentences under facts.json "_shared"): there is no client name, so
"paraphrased" means the expected value (for a standard: the required phrase) appears anywhere in one message/item
of the request (a number of days must appear as "<n> days" or "<n>-day"); "verbatim" is still the exact sentence.
verdict (combined with the grade outcome):
  pass + verbatim/paraphrased   ok
  pass + absent                 lucky (guess or leak; inspect)
  fail + absent                 retrieval_miss  (compaction dropped it, AG did not page it in)
  fail + verbatim/paraphrased   usage_miss      (it was there, the model did not use it)
UNK checks: presence is "n/a"; verdict is ok (null) or made_up.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

WRITE_TOOLS = ("write", "edit", "create")


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().casefold()


def messages_text(req):
    """list of per-message strings from a request payload (tolerates str or list-of-parts content)"""
    out = []
    if isinstance(req.get("body"), dict):  # bench proxy wrapper: {call_id, turn, ..., body}
        req = req["body"]
    if "contents" in req:  # Gemini generateContent body
        def ptext(p):
            if "text" in p:
                return p["text"]
            for k in ("functionCall", "functionResponse"):
                if k in p:
                    return json.dumps(p[k], ensure_ascii=False)
            return ""
        si = req.get("systemInstruction") or req.get("system_instruction")
        if si:
            out.append("\n".join(ptext(p) for p in si.get("parts", [])))
        for c in req["contents"]:
            out.append("\n".join(ptext(p) for p in c.get("parts", [])))
        return out
    sysm = req.get("system")
    if isinstance(sysm, str):
        out.append(sysm)
    for m in req.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict):
                    parts.append(str(p.get("text") or p.get("content") or p.get("input") or ""))
            out.append("\n".join(parts))
        elif c is not None:
            out.append(json.dumps(c))
    return out


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def writing_call(calls_by_turn, turn):
    cs = calls_by_turn.get(turn, [])
    if not cs:
        return None
    w = [c for c in cs if any(t.lower() in WRITE_TOOLS or any(x in t.lower() for x in WRITE_TOOLS)
                              for t in (c.get("tool_calls") or []))]
    return (w or cs)[-1]


def value_forms(v):
    """strings that count as the value appearing in text"""
    if v is None:
        return []
    forms = {norm(v)}
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        forms |= {f"{v}%", f"{v:,}" if isinstance(v, int) else str(v)}
    return [f for f in forms if f]


def shared_forms(chk):
    """strings that count as a shared fact's value (or a standard's required phrase) appearing in text"""
    if chk["kind"] == "style":
        return [norm(chk["expected"]["include"])]
    v = chk["expected"]
    if chk["field"] == "quote_valid_days":
        return [f"{v} days", f"{v}-day", f"{v} day"]
    return [norm(v)]


def classify(chk, msgs, source_sentence):
    texts = [norm(m) for m in msgs]
    if chk.get("scope") == "shared":
        if source_sentence and any(norm(source_sentence) in t for t in texts):
            return "verbatim"
        forms = shared_forms(chk)
        if any(re.search(r"(?<![\w])" + re.escape(f) + r"(?![\w])", t) for t in texts for f in forms):
            return "paraphrased"
        return "absent"
    client = norm(chk["client"])
    if chk["kind"] == "style":
        pref = norm(chk["expected"]["include"])
        if source_sentence and any(norm(source_sentence) in t for t in texts):
            return "verbatim"
        if any(pref in t and client in t for t in texts):
            return "paraphrased"
        return "absent"
    if source_sentence and any(norm(source_sentence) in t for t in texts):
        return "verbatim"
    forms = value_forms(chk["expected"])
    for t in texts:
        if client in t and any(re.search(r"(?<![\w.])" + re.escape(f) + r"(?![\w.])", t) for f in forms):
            return "paraphrased"
    return "absent"


def verdict(outcome, presence):
    if presence == "n/a":
        return "ok" if outcome == "pass" else "made_up"
    if outcome == "pass":
        return "ok" if presence != "absent" else "lucky"
    return "retrieval_miss" if presence == "absent" else "usage_miss"


def attribute_run(session, run, facts, checks):
    grades = load_jsonl(os.path.join(run, "grades.jsonl"))
    calls = load_jsonl(os.path.join(run, "trace", "calls.jsonl"))
    by_turn = defaultdict(list)
    for c in calls:
        by_turn[c["turn"]].append(c)
    chk_by_id = {c["check_id"]: c for c in checks}
    cache, rows = {}, []
    for g in grades:
        chk = chk_by_id[g["check_id"]]
        call = writing_call(by_turn, g["turn"])
        row = {"check_id": g["check_id"], "turn": g["turn"], "client": g["client"], "field": g["field"],
               "tags": g["tags"], "placement": g["placement"], "age_turns": g["age_turns"],
               "outcome": g["outcome"], "call_id": call["call_id"] if call else None,
               "scope": chk.get("scope", "client"), "cross_client": chk.get("cross_client")}
        if g["kind"] == "unknown":
            row.update(presence="n/a", verdict=verdict(g["outcome"], "n/a"))
            rows.append(row)
            continue
        if call is None:
            row.update(presence=None, verdict="no_call")
            rows.append(row)
            continue
        rf = os.path.join(run, call["request_file"])
        if not os.path.exists(rf):
            row.update(presence=None, verdict="no_request_file")
            rows.append(row)
            continue
        if rf not in cache:
            cache[rf] = messages_text(json.load(open(rf)))
        src = None
        if (g["kind"] == "field" or chk.get("scope") == "shared") and chk.get("stated_turn") is not None:
            pool = (facts.get("_shared", {}) if chk.get("scope") == "shared" else facts.get(chk["client"], {}))
            for fv in pool.get(chk["field"], []):
                if fv["turn"] == chk["stated_turn"]:
                    src = fv["sentence"]
        presence = classify(chk, cache[rf], src)
        row.update(presence=presence, verdict=verdict(g["outcome"], presence))
        rows.append(row)
    with open(os.path.join(run, "attribution.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return rows


def table(rows):
    c = Counter(r["verdict"] for r in rows if r["presence"] != "n/a")
    n = sum(c.values())
    fails = c["retrieval_miss"] + c["usage_miss"]
    s = "  " + ", ".join(f"{k}={v}" for k, v in sorted(c.items()))
    if fails:
        s += f"   | of {fails} failures: retrieval {c['retrieval_miss']/fails:.0%}, usage {c['usage_miss']/fails:.0%}"
    return s + f"   (n={n})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--run")
    ap.add_argument("--all")
    a = ap.parse_args()
    checks = load_jsonl(os.path.join(a.session, "checks.jsonl"))
    facts = json.load(open(os.path.join(a.session, "facts.json")))
    runs = []
    if a.run:
        runs.append(a.run)
    if a.all:
        for dp, dn, fn in os.walk(a.all):
            if "run.json" in fn:
                runs.append(dp)
                dn[:] = []
    if not runs:
        sys.exit("give --run or --all")
    for r in sorted(runs):
        print(os.path.basename(r))
        print(table(attribute_run(a.session, r, facts, checks)))


if __name__ == "__main__":
    main()
