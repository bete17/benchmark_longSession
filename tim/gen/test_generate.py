"""Invariant tests for generator v2.  Run: python -m unittest gen/test_generate.py  (from tim/)"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import generate as g  # noqa: E402


def build(seed):
    out = tempfile.mkdtemp()
    subprocess.run([sys.executable, os.path.join(HERE, "generate.py"), "--seed", str(seed), "--out", out],
                   check=True, capture_output=True)
    rd = lambda n: [json.loads(l) for l in open(os.path.join(out, n))]
    return rd("session.jsonl"), rd("checks.jsonl"), json.load(open(os.path.join(out, "facts.json")))


class V2Invariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sessions = {s: build(s) for s in (0, 1)}

    def test_sentence_verbatim_in_message_at_turn(self):
        for seed, (msgs, checks, facts) in self.sessions.items():
            text = {m["turn"]: m["text"] for m in msgs}
            n = 0
            for c, fs in facts.items():
                for f, hs in fs.items():
                    for h in hs:
                        n += 1
                        self.assertIn(h["sentence"], text[h["turn"]], (seed, c, f, h["turn"]))
                        self.assertIn(h["form"], ("sentence", "buried"))
            self.assertGreaterEqual(n, 80)

    def test_check_fields_and_bins(self):
        for seed, (msgs, checks, facts) in self.sessions.items():
            for c in checks:
                self.assertIn("age_bin", c)
                self.assertEqual(c["session_bin"], (c["turn"] - 1) // 30)
                if c["kind"] == "unknown":
                    self.assertIsNone(c["age_bin"])
                else:
                    self.assertEqual(c["age_bin"], g.age_bin_of(c["age_turns"]))
                    self.assertIn(c["form"], ("sentence", "buried"))

    def test_every_deliverable_has_fresh_medium_old(self):
        for seed, (msgs, checks, facts) in self.sessions.items():
            by = {}
            for c in checks:
                if c["kind"] == "field" and "CTRL" not in c["tags"]:
                    by.setdefault(c["deliverable_id"], {}).setdefault(c["age_bin"], 0)
                    by[c["deliverable_id"]][c["age_bin"]] += 1
            turns = {c["deliverable_id"]: c["turn"] for c in checks}
            for d, bins in by.items():
                self.assertIn("fresh", bins, d)
                if turns[d] >= g.MEDIUM_REQUIRED_FROM:
                    self.assertIn("medium", bins, d)
                if turns[d] >= g.OLD_REQUIRED_FROM:
                    self.assertIn("old", bins, d)

    def test_balance_criteria(self):
        """Inside each age bin the field group, placement and form mix must not depend on the bin."""
        for seed, (msgs, checks, facts) in self.sessions.items():
            self.assertEqual(g.balance_problems(checks), [], seed)
            st = g.balance_stats(checks)
            for key, lim in (("sup", 0.15), ("emb", 0.10), ("bur", 0.10)):
                vals = [st["bins"][b][key] for b in g.BAL_BINS]
                self.assertLessEqual(max(vals) - min(vals), lim, (seed, key, vals))
            for f, row in st["fields"].items():
                self.assertGreaterEqual(sum(1 for b in g.BAL_BINS if row[b] > 0), 2, (seed, f, row))
            sup = sum(len(hs) - 1 for fs in facts.values() for hs in fs.values())
            self.assertGreaterEqual(sup, 30, seed)

    def test_size(self):
        for seed, (msgs, checks, facts) in self.sessions.items():
            self.assertTrue(140 <= len(msgs) <= 170)


if __name__ == "__main__":
    unittest.main()
