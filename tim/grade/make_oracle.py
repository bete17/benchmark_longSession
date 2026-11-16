"""Write synthetic deliverables to sanity-check the grader.

  --mode perfect   every header value is the current fact, body follows style rules  -> expect 100%
  --mode stale     every fact uses its FIRST stated value                             -> D checks fail as "stale"
  --mode empty     write nothing                                                      -> all "missing_file"

Usage: python make_oracle.py --session ../sessions/pilot_seed0 --out /tmp/oracle_perfect --mode perfect
"""

import argparse
import json
import os
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["perfect", "stale", "empty"], default="perfect")
    a = ap.parse_args()
    checks = [json.loads(l) for l in open(os.path.join(a.session, "checks.jsonl"))]
    by_d = defaultdict(list)
    for c in checks:
        by_d[c["deliverable_id"]].append(c)
    os.makedirs(os.path.join(a.out, "deliverables"), exist_ok=True)
    if a.mode == "empty":
        return
    for did, cs in by_d.items():
        hdr = {"deliverable_id": did, "client": cs[0]["client"], "type": cs[0]["type"]}
        body = "Hello,\n\nThanks for a great quarter."
        for c in cs:
            if c["kind"] == "style":
                body += f" We'd like to talk about the {c['expected']['include']}."
                continue
            v = c["expected"]
            if a.mode == "stale" and c["superseded_values"]:
                v = c["superseded_values"][0]
            hdr[c["field"]] = v
        with open(os.path.join(a.out, "deliverables", f"{did}.md"), "w") as f:
            f.write("```json\n" + json.dumps(hdr, indent=1) + "\n```\n\n" + body + "\n")


if __name__ == "__main__":
    main()
