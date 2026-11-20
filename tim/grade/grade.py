"""Grade one run's deliverables against a generated session's checks.

Usage:
  python grade.py --session ../sessions/pilot_seed0 --run ../results/<run_dir> [--label NAME]

<run_dir>/deliverables/<id>.md is what the agent wrote. Each file must start with a ```json block
(the header), followed by the body. Writes <run_dir>/grades.jsonl (one row per check) and
<run_dir>/summary.json, and prints a summary table.

Outcomes per check:
  pass          header value (or body, for style checks) matches the current fact
  stale         header value equals a superseded value of the fact (a D failure mode)
  wrong         present but neither current nor superseded
  missing_key   header lacks the key
  no_header     file exists but has no parsable ```json block
  missing_file  deliverable never written
  made_up       (UNK checks) a value was given for a fact never stated; null or "unknown" passes
Style checks pass only if the body includes the client's preferred word and excludes the banned
one (case-insensitive). The header is not scanned, since keys like discount_pct contain banned words.

Shared (G) checks, scope "shared": facts that apply to every client (company-wide rules graded as header
fields; the user's own standards graded in the body like style checks). A standard with an empty "exclude"
only needs the include string; with mode "heading" the include string must be a line of its own (optionally a
markdown heading or bold). A superseded sign-off phrase still present in the body is graded "stale". summary.json
keeps every earlier key, now computed on client-scope checks only for the age, session and form tables (so the
balanced per-client design is untouched), and adds by_scope, by_cross_client (G checks: said during the same
client's visit vs a different client's visit) and *_shared versions of the age, session and form tables.
"""

import argparse
import json
import os
import re
from collections import defaultdict

FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n```", re.S)


def parse(path):
    """(header dict | None, body str | None, error | None)"""
    if not os.path.exists(path):
        return None, None, "missing_file"
    text = open(path, encoding="utf-8", errors="replace").read()
    m = FENCE.search(text)
    if not m:
        return None, text, "no_header"
    try:
        hdr = json.loads(m.group(1))
        if not isinstance(hdr, dict):
            raise ValueError
    except Exception:
        return None, text[m.end():], "no_header"
    return hdr, text[m.end():], None


def norm(field, v):
    if v is None:
        return None
    if field in ("seats", "term_months", "discount_pct", "quote_valid_days"):
        s = str(v).strip().rstrip("%").replace(",", "")
        s = re.sub(r"[^\d.\-]", "", s.split()[0]) if s else s
        try:
            return int(round(float(s)))
        except ValueError:
            return str(v).strip().casefold()
    s = str(v).strip().casefold()
    s = re.sub(r"^(mr|ms|mrs|dr)\.?\s+", "", s)
    return re.sub(r"\s+", " ", s)


UNKNOWN_OK = {None, "", "null", "none", "unknown", "n/a", "na", "tbd"}


def grade_check(chk, hdr, body, err):
    if chk["kind"] == "style":
        if err == "missing_file":
            return "missing_file"
        b = (body or "").casefold()
        inc, exc = chk["expected"]["include"].casefold(), (chk["expected"].get("exclude") or "").casefold()
        if exc and exc in b:
            return "stale" if chk["expected"]["exclude"] in chk.get("superseded_values", []) else "wrong"
        if chk["expected"].get("mode") == "heading":
            pat = r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*|__)?[ \t]*" + re.escape(inc) + r"[ \t]*:?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*$"
            return "pass" if re.search(pat, b, re.M) else "wrong"
        return "pass" if inc in b else "wrong"
    if err:
        return err
    f = chk["field"]
    if chk["kind"] == "unknown":
        if f not in hdr:
            return "missing_key"
        v = hdr[f]
        return "pass" if (v is None or str(v).strip().casefold() in UNKNOWN_OK) else "made_up"
    if f not in hdr:
        return "missing_key"
    got = norm(f, hdr[f])
    if got == norm(f, chk["expected"]):
        return "pass"
    if any(got == norm(f, s) for s in chk.get("superseded_values", [])):
        return "stale"
    return "wrong"


def age_bin(a):
    if a is None:
        return "none"
    for lo, hi in [(0, 10), (10, 30), (30, 60), (60, 100), (100, 10**9)]:
        if lo <= a < hi:
            return f"{lo}-{hi - 1 if hi < 10**9 else ''}"


def age_bin_v2(a):
    """fresh 2-10, medium 15-45, old 60+, other (same thresholds as gen/generate.py)"""
    if a is None:
        return None
    return "fresh" if 2 <= a <= 10 else "medium" if 15 <= a <= 45 else "old" if a >= 60 else "other"


def summarize(all_rows):
    """Main metrics use checks on facts that were stated (kind field or style). UNK checks (fields
    asked for before their fact was stated) are reported separately as a made-up-value rate."""
    def rate(rs):
        n = len(rs)
        return {"n": n, "pass": sum(r["outcome"] == "pass" for r in rs) / n if n else None}
    for r in all_rows:
        r.setdefault("scope", "client")
    rows = [r for r in all_rows if r["kind"] != "unknown"]
    unk = [r for r in all_rows if r["kind"] == "unknown"]
    # Binned views (age bin, session bin, form) leave out CTRL checks: their value is restated in the
    # request, so their pass rate says nothing about memory at that age. v1 sessions carry no age_bin,
    # so it is derived from age_turns.
    binned_all = [r for r in rows if "CTRL" not in r["tags"]]
    binned = [r for r in binned_all if r["scope"] == "client"]
    binned_g = [r for r in binned_all if r["scope"] == "shared"]
    for r in binned_all:
        if r.get("age_bin") is None:
            r["age_bin"] = age_bin_v2(r["age_turns"])
        if r.get("session_bin") is None:
            r["session_bin"] = (r["turn"] - 1) // 30

    def by(keyf, src=None):
        b = defaultdict(list)
        for r in (binned if src is None else src):
            b[keyf(r)].append(r)
        return {k: rate(v) for k, v in sorted(b.items(), key=lambda kv: str(kv[0]))}

    def x_table(src):
        cross = defaultdict(lambda: defaultdict(list))
        for r in src:
            cross[str(r["session_bin"])][r["age_bin"]].append(r)
        return {sb: {ab: rate(v) for ab, v in sorted(d.items())} for sb, d in sorted(cross.items())}

    cross = defaultdict(lambda: defaultdict(list))
    for r in binned:
        cross[str(r["session_bin"])][r["age_bin"]].append(r)
    shared_rows = [r for r in rows if r["scope"] == "shared"]
    unk_g = [r for r in unk if r["scope"] == "shared"]
    out = {"overall": rate(rows),
           "overall_client": rate([r for r in rows if r["scope"] == "client"]),
           "by_scope": {"client": rate([r for r in rows if r["scope"] == "client"]), "shared": rate(shared_rows)},
           "by_cross_client": {"same_client": rate([r for r in shared_rows if not r.get("cross_client")]),
                               "cross_client": rate([r for r in shared_rows if r.get("cross_client")])},
           "by_cross_client_x_age_bin": {k: by(lambda r: r["age_bin"], [r for r in binned_g if bool(r.get("cross_client")) == (k == "cross_client")])
                                         for k in ("same_client", "cross_client")},
           "by_age_bin_shared": by(lambda r: r["age_bin"], binned_g),
           "by_session_bin_shared": by(lambda r: str(r["session_bin"]), binned_g),
           "by_session_bin_x_age_bin_shared": x_table(binned_g),
           "by_form_shared": by(lambda r: r.get("form") or "n/a", binned_g),
           "unknown_fields_shared": {"n": len(unk_g), "null_rate": rate(unk_g)["pass"],
                                     "made_up_rate": (sum(r["outcome"] == "made_up" for r in unk_g) / len(unk_g)) if unk_g else None},
           "by_age_bin": by(lambda r: r["age_bin"]),
           "by_session_bin": by(lambda r: str(r["session_bin"])),
           "by_session_bin_x_age_bin": x_table(binned),
           "by_form": by(lambda r: r.get("form") or "n/a"),
           "bin_tables_note": "age_bin, session_bin and form tables exclude CTRL and UNK checks and cover client-scope "
                              "checks only; the *_shared tables hold the G checks",
           "unknown_fields": {"n": len(unk), "null_rate": rate(unk)["pass"],
                              "made_up_rate": (sum(r["outcome"] == "made_up" for r in unk) / len(unk)) if unk else None}}
    # Shared (G) rows enter by_tag only as "G" (and "G+D" when the value was changed), so the per-client tags A, C, D,
    # CTRL, NEAR keep their meaning; by_age_turns covers client rows only (the shared age view is by_age_bin_shared).
    groups = {"by_tag": lambda r: (["G"] + (["G+D"] if "D" in r["tags"] else [])) if r["scope"] == "shared" else r["tags"], "by_placement": lambda r: [r["placement"]],
              "by_field": lambda r: [r["field"]], "by_age_turns": lambda r: [age_bin(r["age_turns"])] if r["scope"] == "client" else [],
              "by_outcome": None}
    for g, key in groups.items():
        if key is None:
            c = defaultdict(int)
            for r in rows:
                c[r["outcome"]] += 1
            out[g] = dict(c)
            continue
        buckets = defaultdict(list)
        for r in rows:
            for k in key(r):
                buckets[k].append(r)
        out[g] = {k: rate(v) for k, v in sorted(buckets.items())}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--label", default=None)
    a = ap.parse_args()
    checks = [json.loads(l) for l in open(os.path.join(a.session, "checks.jsonl"))]
    cache = {}
    rows = []
    for chk in checks:
        did = chk["deliverable_id"]
        if did not in cache:
            cache[did] = parse(os.path.join(a.run, "deliverables", f"{did}.md"))
        hdr, body, err = cache[did]
        rows.append({**{k: chk[k] for k in ("check_id", "deliverable_id", "client", "field", "kind",
                                              "turn", "age_turns", "placement", "tags",
                                              "visits_since_client_last_seen")},
                     "scope": chk.get("scope", "client"), "cross_client": chk.get("cross_client"),
                     "placement_client": chk.get("placement_client"),
                     "age_bin": chk.get("age_bin"), "session_bin": chk.get("session_bin"),
                     "form": chk.get("form"),
                     "expected": chk["expected"],
                     "got": (hdr or {}).get(chk["field"]) if chk["kind"] in ("field", "unknown") else None,
                     "outcome": grade_check(chk, hdr, body, err)})
    with open(os.path.join(a.run, "grades.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    s = summarize(rows)
    s["label"] = a.label or os.path.basename(os.path.normpath(a.run))
    with open(os.path.join(a.run, "summary.json"), "w") as f:
        json.dump(s, f, indent=1)
    fmt = lambda v: "n/a" if v["pass"] is None else f"{v['pass']:.2f}"
    print(f"{s['label']}: overall {s['overall']['pass']:.3f} (n={s['overall']['n']})")
    for g in ("by_scope", "by_cross_client", "by_age_bin_shared", "by_tag", "by_placement", "by_age_turns", "by_age_bin",
              "by_session_bin", "by_form"):
        print(f"  {g}: " + ", ".join(f"{k}={fmt(v)}({v['n']})" for k, v in s[g].items()))
    for sb, d in s["by_session_bin_x_age_bin"].items():
        print(f"  session_bin {sb}: " + ", ".join(f"{k}={fmt(v)}({v['n']})" for k, v in d.items()))
    print(f"  outcomes: {s['by_outcome']}")
    u = s["unknown_fields"]
    if u["n"]:
        print(f"  unknown fields: n={u['n']} null={u['null_rate']:.2f} made_up={u['made_up_rate']:.2f}")


if __name__ == "__main__":
    main()
