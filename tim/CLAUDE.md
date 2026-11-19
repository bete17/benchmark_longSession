# Instructions for Claude Code working in tim/

You are continuing Tim's experiment on long agent sessions. The design and steps 0 and 1 were done
in a separate Claude session (claude.ai); you run steps 2 to 4 on Tim's laptop. Read this file and
`DESIGN.md` fully before doing anything.

## Who and why

Tim is an undergraduate research assistant in Asim Biswal's lab at UC Berkeley (with Sepanta
Zeighami, author of Actor-Gatherer). The lab question: in which scenarios does Actor-Gatherer (AG) beat
a regular agent? This experiment targets one long session where a user gives related prompts that drift
across several workstreams, and measures accuracy, degradation over the session, and cost.

Writing rule from Tim: no em dashes in any prose you write (docs, logs, reports). Use commas,
colons, or separate sentences.

## What exists already

```
tim/
  DESIGN.md                    the experiment design (hypotheses A, C, D, E, CTRL; conditions; metrics)
  CLAUDE.md                    this file
  gen/generate.py              seeded session generator (stdlib only)
  grade/grade.py               grader (stdlib only)
  grade/make_oracle.py         synthetic deliverables to sanity-check the grader
  TRACE.md                     the trace format every run must produce (read before writing run code)
  run/system_prompt.md         the system prompt for the Actor in EVERY condition (replaces pi's coding prompt)
  gen/report.py                prints the v2 schedule report (counts, session_bin x age_bin table, per-deliverable bins)
  gen/test_generate.py         invariant tests (each fact sentence is verbatim in its turn's message, bins, size)
  sessions/pilot_v2_seed0/     the v2 session (fixed-age check schedule, see DESIGN.md): 149 user messages,
                               254 checks over 38 deliverables (226 on stated facts: A 84 / C 134 / D 89 /
                               CTRL 10 / NEAR 20; 28 UNK); checks also carry age_bin, session_bin, form
  sessions/pilot_seed0/        the v1 pilot session (kept for the step 2 smoke runs)
    session.jsonl              145 user messages, in order: {"turn","visit","client","visit_kind","text"}
    checks.jsonl               163 checks over 32 deliverables: 129 on stated facts, tagged A / C / D /
                               CTRL / NEAR, plus 34 UNK checks (field asked before its fact was stated;
                               the right answer is null, anything else counts as made up)
    facts.json, visits.json    ground truth and the intended visit boundaries
    readable.md                the session as a readable script with planted facts listed per visit
    meta.json                  counts and a character-based size estimate (~60k user tokens)
```

Verified before handoff (v1): `make_oracle.py --mode perfect` grades 100% (and null on all 34 UNK fields), `--mode stale` fails exactly
the 14 D checks as "stale", `--mode empty` grades 0%.

The agent under test sees only each message's `text`, sent in order. Deliverables are files the agent
writes to `deliverables/<id>.md` in its working directory, starting with a ```json header. The grader
reads `<run_dir>/deliverables/`.

## Actor-Gatherer facts you need (repo github.com/ucbepic/actor-gatherer, read at commit 59ca484)

- Install AG pinned (`pip install git+https://github.com/ucbepic/actor-gatherer@59ca484` or a clone
  checked out at that commit). Do not copy AG source into this repo. If you must patch AG, do it in a
  separate clone and record the diff in RUNLOG.md.
- Harnesses: `ag/cli/harnesses.toml` lists claude, codex, pi, opencode, antigravity. We use **pi**
  (`ag pi ...`). Pi gets AG as an extension (`ag/integrations/harnesses/pi/setup/setup.ts`).
- Gatherer model: any `provider/model` through LiteLLM, e.g. `gemini/<model>`, with `GEMINI_API_KEY`.
- **One Gatherer per run.** Each AG run uses exactly one gatherer, and the whole session (all clients,
  all tasks) goes through one pi session, which is one channel in that gatherer. AG splits the history
  into tasks internally; that is not multiple gatherers. Create a fresh gatherer for each new run so
  runs never share state.
- Policy: `proactive_context_manager`, options in `ag/ag_policies/proactive_context_manager/config.py`,
  overridden per gatherer under `[options]` in `~/.ag/gatherers/<name>/gatherer.toml`. A gatherer's
  policy cannot change once used.
- Knobs that matter here: `max_context_tokens` (set 100000), `min_context_tokens` (15000),
  `keep_previous_task_tokens`, `keep_temporal_relevant_tokens`, `keep_task_relevant_tokens` (15000 each),
  `all_task_tokens` (30000), `summarize` (true), `boundary_cooldown` (2), `check_boundaries` (true).
  Also counted in the budget: `parallel_index_tokens` (10000), `keep_so_far_tokens` (15000),
  `keep_pinned_tokens` (5000). AG requires section budgets + keep_pinned_tokens < max_context_tokens;
  the defaults sum to 105000, so defaults plus `max_context_tokens = 100000` is refused (fixed
  2026-11-17, see RUNLOG.md: the bench scales every budget by 0.5, AG's own `within_limit` rule).
- When AG builds context: under `min_context_tokens` never; above it, after `boundary_cooldown` user
  messages into the open task, one model call asks whether the new message starts a new task
  (`ag/ag_policies/tools/boundary.py`, biased to "same task"); over `max_context_tokens` a build is forced.
- Pi compaction: pi compacts past `contextWindow - compaction.reserveTokens` (pi settings.json,
  default reserve 16384). pi's compaction summary asks for 0.8 * reserveTokens output tokens, so the
  bench registers the model with `contextWindow = 116384` and keeps reserve 16384 (still
  reserve = contextWindow - 100000), rather than a 1M window with a ~948k reserve. Under AG, pi's compaction is cancelled while AG answers
  (`AG_PI_COMPACTION=allow` overrides). AG infers pi's limit as window minus 16384 times 0.9 unless
  `max_context_tokens` is set, so **set `max_context_tokens = 100000` explicitly** for AG runs.
- Logs: AG's own model calls (purposes like boundary, segment, rank, summarize) go through
  `ag/ag_tools/llm/calllog.py` with usage; per-call context sizes go to the gatherer's `server.log`.
  Pi records usage on each assistant message in its session file, but not for its compaction summary
  calls; the bench's logging proxy (run/run_session.py) records every Actor call instead.

## Your steps (from DESIGN.md section 8)

Keep an append-only log at `tim/RUNLOG.md`: date, what you ran, what happened, decisions. Commit and
push after each step (`git push origin main`; small commits). Stop at every gate marked ASK TIM.

### Step 2: smoke test
1. Install pi (`@mariozechner/pi-coding-agent`) and AG. Confirm pi runs on Gemini with Tim's key.
2. Find how to drive pi non-interactively for many turns in ONE session (check `pi --help`: an RPC or
   print mode with session continuation). The driver must send message N only after the agent finishes
   message N-1, including its tool calls.
3. Replace pi's default (coding-agent) system prompt with `run/system_prompt.md` in every condition,
   keeping pi's file tools so the agent can write deliverables. Check `pi --help` for the flag that
   replaces the system prompt (not one that appends). Verify (a) the file tools still work and
   (b) under `ag pi` the replaced system prompt still reaches the model (inspect one request in AG's
   server.log or the pi session).
4. Confirm pi compaction can be forced at a 100k ceiling via `compaction.reserveTokens`
   (reserve = contextWindow - 100000 for the chosen model) and that compaction events are visible in
   the session file.
5. Confirm `ag pi` works on Gemini for both Actor and Gatherer, and that you can collect: Actor usage
   per call, Gatherer usage per call (by purpose), and context size per call.
6. Write `tim/run/run_session.py` and `tim/trace/export.py` to produce exactly the layout in
   **`tim/TRACE.md`** (read it fully): raw material under `raw/` (gitignored), the normalized trace
   under `trace/`, `run.json`, and `cost.json` with `actor_usd`, `gatherer_usd`, `total_usd` (prices in
   `tim/run/prices.json`, cite the source page). The key requirement is capturing the exact request
   every Actor call sent, the same way in every condition. A viewer and the attribution analysis are
   being built against TRACE.md in parallel, so do not change field names; if a field cannot be filled,
   leave it null and say why in RUNLOG.md.
7. Measure the real token size of the pilot session after a short run. If a full run would not pass
   100k of actor context at least twice under pi compaction, regenerate with a larger
   `--report-rows` (main size lever) and note it. Do not change other generator semantics without
   asking.
ASK TIM: report model choice, measured cost of the smoke run, and projected pilot cost, before step 3.

### Step 3: granularity check
Use `sessions/pilot_v2_seed0` (the step 2 smoke runs used v1, `sessions/pilot_seed0`).

Check that AG's task segmentation and boundary check cut the session where `visits.json` intends.
Preferred: a script `tim/run/granularity.py` that feeds the scripted user messages (with short
placeholder assistant replies) through AG's own segmentation and boundary tools
(`ag/ag_policies/tools/segmentation.py`, `boundary.py`) with the Gatherer model, and reports where it
cuts vs visit boundaries (precision and recall of boundaries, and which visits got merged).
Fallback: one AG run on the first ~40 turns, reading boundary verdicts from AG's logs.
ASK TIM if fewer than about 80% of visit switches are detected, with examples, before changing the
generator.

### Step 4: pilot runs
Run on `sessions/pilot_v2_seed0`; before the full runs, do a short v2 run and check fact coverage (`analysis/coverage.py`) to settle the compaction ceiling (DESIGN.md open items).

Conditions (same Actor model, same session, same 100k ceiling, 2 runs each):
- `pi_compact`: pi alone, compaction at 100k.
- `pi_per_client`: one pi session per client (route each visit's messages to that client's session),
  compaction at 100k. Models the "separate threads" workaround. Embedded facts land in whichever
  client's thread the message belongs to, as they would for a real user.
- `ag_half`: `ag pi`, `max_context_tokens = 100000` with every AG budget halved (min_context 7500,
  keep_pinned 2500, all_task 15000, parallel_index 5000, keep_* 7500, keep_so_far 7500). Renamed from
  `ag_default` on 2026-11-19: AG's defaults do not fit under 100k.
- `ag_quarter`: as ag_half with `keep_*_relevant_tokens` and `keep_previous_task_tokens` at 3750
  (renamed from `ag_tight`). run.json records every effective AG option in `ag_effective_options`.
  (One knob variant for the spend curve; more later.)
- `pi_full`: pi alone, compaction off, model registered with its real window (1,048,576), full
  session, 1 run. Context-rot reference (added 2026-11-19; the session fits, so no truncation).
Grade each run: `python grade/grade.py --session sessions/pilot_v2_seed0 --run results/<run>`.
ASK TIM before any spend beyond the cap he gives you.

## What to hand back

`tim/results/PILOT.md`: a table of pass rate overall and by tag (A, C, D, CTRL, NEAR), by placement
(home, embedded) and by age bin, per condition, with the three cost numbers and latency; notes on
anything odd (missing files, format failures, crashes); and for AG runs, 3 to 5 failed checks traced
to what AG did or did not page in. Tim or the claude.ai session will do plots and the write-up.

## Ground rules

- Never put API keys in the repo or in logs you commit. Add `.env` and raw secrets paths to `.gitignore`.
- Raw pi session files and AG databases can be large: commit them only if under ~20 MB per run,
  otherwise commit a manifest and keep them local.
- If something in this file turns out to be wrong (a flag name, a path), fix this file and say so in
  RUNLOG.md.
