# Prompt to start Claude Code

Run Claude Code from the repo's `tim/` folder (so `tim/CLAUDE.md` loads), after `git pull`, with
`GEMINI_API_KEY` set in your shell. Then paste:

---

You're picking up my long-session benchmark for Actor-Gatherer. A separate Claude session wrote the
design, a session generator, a grader, and a pilot session, all in this folder. Read CLAUDE.md and
DESIGN.md completely first, then skim sessions/pilot_seed0/readable.md and run the grader's oracle
self-test (grade/make_oracle.py in perfect, stale and empty modes) to confirm the setup works on this
machine.

Then do step 2 (smoke test) from CLAUDE.md: install pi and Actor-Gatherer pinned at commit 59ca484,
get pi and `ag pi` running on Gemini with my GEMINI_API_KEY, work out how to drive pi through many
turns in one session, confirm we can force compaction at a 100k ceiling, write tim/run/run_session.py,
and measure the real token size and cost of a short run.

Use <ACTOR_MODEL> for the actor and <GATHERER_MODEL> for the gatherer. My spend cap for everything
before I approve the pilot is $<SMOKE_CAP>, and for the pilot itself $<PILOT_CAP>.

Keep tim/RUNLOG.md updated as you go and commit and push after each step. Stop and report back to me
at each ASK TIM gate in CLAUDE.md: what worked, what didn't, measured costs, and your projection for
the next step. Don't use em dashes in anything you write. If anything in CLAUDE.md is wrong, fix it and
note the fix in RUNLOG.md.

---

Fill in the four placeholders before pasting.
