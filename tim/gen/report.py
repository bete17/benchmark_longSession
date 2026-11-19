"""Report on a generated v2 session: does it meet the fixed-age check schedule?

  python gen/report.py --session sessions/pilot_v2_seed0 [--baseline sessions/pilot_seed0] [--strict]

Prints fact counts (statements, supersessions, embedded and buried shares, share per third of the
session), the check table by session bin (rows) x age bin (columns), and one line per deliverable
confirming it holds at least one fresh check (and medium and old ones once the session is long
enough). With --strict the exit code is 1 when a requirement is not met.

The balance section prints, per age bin, the share of checks on supersedable fields, embedded facts and buried
facts, and a field x age_bin table; --strict fails when any of these spreads exceeds its limit (field group 15
points, placement 10, form 10) or a field appears in fewer than two of the three bins.

Age bins (age = deliverable turn minus the turn the current value was stated):
  fresh 2 to 10, medium 15 to 45, old 60+, other = everything between (and age 0 or 1).
CTRL checks (value restated in the request) and UNK checks (answer is null) are listed separately
and never count toward a bin.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from generate import (MEDIUM_REQUIRED_FROM, OLD_REQUIRED_FROM, age_bin_of, session_bin_of, FIELDS,  # noqa: E402
                      SUPERSEDABLE, BAL_BINS, MAX_GROUP_SPREAD, MAX_PLACE_SPREAD, MAX_FORM_SPREAD,
                      MIN_SUPERSESSIONS, balance_problems)

BINS = ["fresh", "medium", "old", "other"]


def load(session):
    rd = lambda n: [json.loads(l) for l in open(os.path.join(session, n))]
    return (rd("session.jsonl"), rd("checks.jsonl"), json.load(open(os.path.join(session, "facts.json"))),
            json.load(open(os.path.join(session, "visits.json"))), json.load(open(os.path.join(session, "meta.json"))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--baseline", default=None, help="a v1 session dir to compare size against")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    msgs, checks, facts, visits, meta = load(a.session)
    problems = []
    n_turns = len(msgs)

    # ---- facts
    stm = [(c, f, i, h) for c, fs in facts.items() for f, hs in fs.items() for i, h in enumerate(hs)]
    n = len(stm)
    sup = sum(1 for _, _, i, _ in stm if i > 0)
    emb = sum(1 for *_, h in stm if h["placement"] == "embedded")
    bur = sum(1 for *_, h in stm if h.get("form") == "buried")
    third = lambda t: min(2, (t - 1) * 3 // n_turns)
    th = Counter(third(h["turn"]) for *_, h in stm)
    sup_th = Counter(third(h["turn"]) for _, _, i, h in stm if i > 0)
    print(f"Session {a.session}: {n_turns} user messages, {len(visits)} visits, "
          f"{len({c['deliverable_id'] for c in checks})} deliverables, {len(checks)} checks")
    print("\nFACTS")
    print(f"  fact statements      {n}")
    print(f"  supersessions        {sup}  (per third of session: {[sup_th[i] for i in range(3)]})")
    print(f"  embedded share       {emb}/{n} = {emb / n:.1%}")
    print(f"  buried share         {bur}/{n} = {bur / n:.1%}")
    print("  statements per third " + ", ".join(f"{['first', 'middle', 'last'][i]}: {th[i]} ({th[i] / n:.1%})" for i in range(3)))
    if not 80 <= n <= 100:
        problems.append(f"fact statements {n} outside 80-100")
    if sup < MIN_SUPERSESSIONS:
        problems.append(f"supersessions {sup} < {MIN_SUPERSESSIONS}")
    if th[0] / n > 0.40:
        problems.append("more than 40% of statements in the first third")
    if not 0.24 <= emb / n <= 0.32:
        problems.append(f"embedded share {emb / n:.1%} outside 24-32%")
    if not 0.27 <= bur / n <= 0.34:
        problems.append(f"buried share {bur / n:.1%} outside 27-34%")

    # ---- verbatim invariant
    text = {m["turn"]: m["text"] for m in msgs}
    bad = [(c, f, h["turn"]) for c, f, _, h in stm if h["sentence"] not in text.get(h["turn"], "")]
    print(f"  sentence found verbatim in its turn's message: {n - len(bad)}/{n}")
    if bad:
        problems.append(f"{len(bad)} fact sentences not verbatim in their message")

    # ---- size
    chars = sum(len(m["text"]) for m in msgs)
    print("\nSIZE")
    print(f"  user messages {n_turns}, user chars {chars:,} (~{chars // 4:,} est. tokens)")
    sizes = Counter(v["last_turn"] - v["first_turn"] + 1 for v in visits)
    print(f"  visit lengths {dict(sorted(sizes.items()))}")
    if min(sizes) < 2 or max(sizes) > 4:
        problems.append("a visit is not 2 to 4 messages long")
    if not 140 <= n_turns <= 170:
        problems.append(f"{n_turns} messages outside 140-170")
    if a.baseline:
        b = json.load(open(os.path.join(a.baseline, "meta.json")))
        r = chars / b["user_text_chars"] - 1
        print(f"  baseline {a.baseline}: {b['user_messages']} messages, {b['user_text_chars']:,} chars; "
              f"this session {r:+.1%} chars, {n_turns - b['user_messages']:+d} messages")
        if abs(r) > 0.15:
            problems.append(f"chars {r:+.1%} vs baseline outside +/-15%")

    # ---- checks table
    field = [c for c in checks if c["kind"] in ("field", "style")]
    ctrl = [c for c in field if "CTRL" in c["tags"]]
    unk = [c for c in checks if c["kind"] == "unknown"]
    main_checks = [c for c in field if "CTRL" not in c["tags"]]
    nd = len({c["deliverable_id"] for c in checks})
    print("\nCHECKS")
    print(f"  field+style {len(field)} (main {len(main_checks)}, CTRL {len(ctrl)}), UNK {len(unk)}; "
          f"CTRL on {len({c['deliverable_id'] for c in ctrl})}/{nd} deliverables")
    tab = defaultdict(Counter)
    for c in main_checks:
        tab[c["session_bin"]][c["age_bin"]] += 1
        if c["age_bin"] != age_bin_of(c["age_turns"]) or c["session_bin"] != session_bin_of(c["turn"]):
            problems.append(f"{c['check_id']}: age_bin or session_bin inconsistent")
    print("  main checks (CTRL and UNK excluded): session_bin (turns) x age_bin")
    print("    " + f"{'session_bin':<16}" + "".join(f"{b:>8}" for b in BINS) + f"{'total':>8}")
    tot = Counter()
    for sb in sorted(tab):
        row = tab[sb]
        tot.update(row)
        print("    " + f"{sb} ({sb * 30 + 1}-{sb * 30 + 30})".ljust(16) + "".join(f"{row[b]:>8}" for b in BINS)
              + f"{sum(row.values()):>8}")
    print("    " + "total".ljust(16) + "".join(f"{tot[b]:>8}" for b in BINS) + f"{sum(tot.values()):>8}")
    tags = Counter(t for c in checks for t in c["tags"])
    print(f"  tags {dict(sorted(tags.items()))}")

    # ---- balance of the mix inside each age bin
    print("\nBALANCE inside each age bin (CTRL and UNK excluded): the mix must not depend on fact age")
    print("    " + f"{'age_bin':<10}{'checks':>8}{'supersedable':>14}{'embedded':>11}{'buried':>9}")
    shares = {k: [] for k in ("sup", "emb", "bur")}
    for b in BINS:
        x = [c for c in main_checks if c["age_bin"] == b]
        m = max(1, len(x))
        sh = (sum(c["field"] in SUPERSEDABLE for c in x) / m, sum(c["placement"] == "embedded" for c in x) / m,
              sum(c["form"] == "buried" for c in x) / m)
        if b in BAL_BINS:
            for k, v in zip(("sup", "emb", "bur"), sh):
                shares[k].append(v)
        print("    " + f"{b:<10}{len(x):>8}{sh[0]:>14.1%}{sh[1]:>11.1%}{sh[2]:>9.1%}" + ("   (not balanced, not in the criteria)" if b == "other" else ""))
    for k, lim, label in (("sup", MAX_GROUP_SPREAD, "supersedable"), ("emb", MAX_PLACE_SPREAD, "embedded"),
                          ("bur", MAX_FORM_SPREAD, "buried")):
        sp = max(shares[k]) - min(shares[k])
        print(f"    spread {label:<13}{sp:>6.1%}  (limit {lim:.0%})  {'ok' if sp <= lim else 'FAIL'}")
    print("  field x age_bin (checks):  group   " + "".join(f"{b:>8}" for b in BINS) + f"{'bins>0':>8}")
    ftab = defaultdict(Counter)
    for c in main_checks:
        ftab[c["field"]][c["age_bin"]] += 1
    for f in FIELDS:
        row = ftab[f]
        print(f"    {f:<18}{'sup' if f in SUPERSEDABLE else 'stable':<8}" + "".join(f"{row[b]:>8}" for b in BINS)
              + f"{sum(1 for b in BAL_BINS if row[b] > 0):>8}")
    problems += balance_problems(checks)

    # ---- per deliverable
    print("\nDELIVERABLES (field checks, CTRL and UNK excluded): counts fresh/medium/old")
    vis_of_turn = {}
    for v in visits:
        for t in range(v["first_turn"], v["last_turn"] + 1):
            vis_of_turn[t] = v
    dels = sorted({c["deliverable_id"] for c in checks})
    turns = []
    ok_f = ok_m = ok_o = req_m = req_o = 0
    for did in dels:
        cs = [c for c in main_checks if c["deliverable_id"] == did and c["kind"] == "field"]
        T = next(c["turn"] for c in checks if c["deliverable_id"] == did)
        turns.append(T)
        cnt = Counter(c["age_bin"] for c in cs)
        need_m, need_o = T >= MEDIUM_REQUIRED_FROM, T >= OLD_REQUIRED_FROM
        req_m += need_m
        req_o += need_o
        f_ok, m_ok, o_ok = cnt["fresh"] > 0, (cnt["medium"] > 0 or not need_m), (cnt["old"] > 0 or not need_o)
        ok_f += f_ok
        ok_m += cnt["medium"] > 0 and need_m
        ok_o += cnt["old"] > 0 and need_o
        v = vis_of_turn[T]
        # at least one fresh fact must come from the same visit (not its request message) or the previous visit
        natural = 0
        for c in cs:
            if c["age_bin"] == "fresh":
                sv = vis_of_turn[c["stated_turn"]]
                if sv["visit"] in (v["visit"], v["visit"] - 1) and c["stated_turn"] != v["first_turn"]:
                    natural += 1
        if natural == 0:
            problems.append(f"{did}: no fresh fact from the same visit or the previous visit")
        flag = "ok" if (f_ok and m_ok and o_ok) else "MISSING"
        if flag != "ok":
            problems.append(f"{did}: fresh={cnt['fresh']} medium={cnt['medium']} old={cnt['old']} (T={T})")
        med = str(cnt["medium"]) if need_m else "n/a"
        old = str(cnt["old"]) if need_o else "n/a"
        print(f"  {did} turn {T:>3} {v['client']:<16} fresh={cnt['fresh']} medium={med} old={old} "
              f"other={cnt['other']}  {flag}")
    print(f"  fresh present: {ok_f}/{len(dels)}; medium present: {ok_m}/{req_m} (required from turn {MEDIUM_REQUIRED_FROM}); "
          f"old present: {ok_o}/{req_o} (required from turn {OLD_REQUIRED_FROM})")
    win = Counter(session_bin_of(t) for t in turns)
    print(f"  first deliverable at turn {min(turns)}; deliverables per 30-turn window "
          f"{[win[i] for i in range(max(win) + 1)]}; gaps min/max {min(b - a_ for a_, b in zip(turns, turns[1:]))}/"
          f"{max(b - a_ for a_, b in zip(turns, turns[1:]))}")
    if min(turns) > 11:
        problems.append("first deliverable after turn 11")
    ctrl_share = len({c['deliverable_id'] for c in ctrl}) / nd
    if not 0.15 <= ctrl_share <= 0.35:
        problems.append(f"CTRL share {ctrl_share:.0%} not about 1 in 4")
    if not unk:
        problems.append("no UNK checks")

    print("\nRESULT: " + ("all requirements met" if not problems else "PROBLEMS"))
    for p in problems:
        print("  - " + p)
    if problems and a.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
