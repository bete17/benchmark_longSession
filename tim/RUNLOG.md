# RUNLOG

Append only. Newest entries at the bottom. Written by Claude Code on Tim's laptop.

## 2026-11-17: setup check

- Read CLAUDE.md, DESIGN.md, TRACE.md, skimmed readable.md.
- Oracle self-test on this machine (python 3.12.2):
  - perfect: overall 1.000 (n=129), UNK n=34 null=1.00 made_up=0.00
  - stale: overall 0.891, D=0.00 (14 of 14 fail as "stale"), everything else as perfect
  - empty: overall 0.000, all missing_file
  Matches the expected results.

## 2026-11-17: step 2, tool install

- Tim's laptop already had an editable AG install (`~/miniconda3`, from a clone at dada1b7, which does
  not contain 59ca484) and an `~/.ag` home. Left both untouched.
- Bench tools live outside the repo in `research/ag-bench/` (run_session.py finds them via
  `BENCH_TOOLS`, default `<repo>/../ag-bench`):
  - `actor-gatherer/`: fresh clone, checked out at 59ca484. Not patched.
  - `venv/`: Python 3.12 venv (uv) with that clone installed editable. Note: a venv named `.venv`
    broke on this Desktop folder, because macOS sets the `hidden` flag on files in dot folders here
    and Python 3.12 skips hidden `.pth` files, so `import ag` failed. Renamed to `venv/`.
  - `node_modules/`: `@mariozechner/pi-coding-agent` 0.73.1 (latest on npm), installed locally.
  - `work/<hash>/workspace/`: the agent's working directory per run (outside the repo so the agent
    cannot read the session's answer files; the folder name is a hash so the condition name is not in
    the system prompt, which includes the cwd).
- Each run gets its own `AG_HOME` under `raw/ag/home/` (fresh gatherer named after the run id, its own
  call logs). Tim's `~/.ag` is never used by the bench.

## 2026-11-17: step 2, model and price

- Models with generateContent on Tim's key include gemini-2.5-flash, 3-flash-preview, 3.5-flash,
  3.6-flash, 3.7-flash, 3.8-flash, and Flash-Lite variants. Picked **gemini-3.8-flash**, the newest
  Flash-tier model, for both Actor and Gatherer.
- Price (https://ai.google.dev/gemini-api/docs/pricing, which redirects to
  https://aistudio.google.com/docs/pricing, read 2026-11-17, paid tier Standard, per 1M tokens):
  input $0.75, output $3.75 (thinking included), cache read $0.075, through 2026-12-31. From
  2027-01-01 the page lists double: $1.50 / $7.50 / $0.15. Stored in `run/prices.json`.
- pi 0.73.1 does not know gemini-3.8-flash, so each run registers it in pi's `models.json` as provider
  `gemini-bench` (api google-generative-ai, apiKey given as the variable name GEMINI_API_KEY, so no
  key is written anywhere).

## 2026-11-17: step 2, pi findings

- Non-interactive multi-turn: `pi --mode rpc`. The driver sends `{"type":"prompt"}`, reads events
  until `agent_end`, then polls `get_state` until `isStreaming` and `isCompacting` are both false
  before sending the next message. One pi process per thread, one session file per thread.
- System prompt: `--system-prompt <file>` replaces pi's coding prompt (a path is read as a file;
  `--append-system-prompt` is the appending one). pi still appends `Current date` and
  `Current working directory` lines. `--no-context-files` is required, otherwise pi walks up from the
  cwd and appends any AGENTS.md or CLAUDE.md it finds. Also `--no-skills --no-prompt-templates
  --no-themes`. Verified in a captured request: systemInstruction is run/system_prompt.md plus the
  date and cwd lines, nothing else.
- Tools: `--tools read,write,edit,ls`. Decision: bash is left out in every condition. It is not a
  file tool, and it would let the agent wander the disk. Side effect: under AG the gatherer guide
  tells the agent about `ag actors` and its history database, which it cannot use without bash.
  Flagged for Tim.
- Thinking: `--thinking low` for the Actor in every condition (pi sends thinkingLevel LOW).
- Compaction: pi compacts when `contextTokens > contextWindow - reserveTokens`. The compaction summary
  call asks for `maxTokens = 0.8 * reserveTokens`, so setting reserve = 1048576 - 100000 would request
  ~760k output tokens. Instead the model is registered with `contextWindow = 116384` and the default
  `reserveTokens = 16384`, which still satisfies reserve = contextWindow - 100000. Fixed this in
  CLAUDE.md.
- pi's compaction summary call does not record its usage anywhere (pi discards it), and does not go
  through the `before_provider_request` extension hook.

## 2026-11-17: step 2, request capture (TRACE.md)

- Method: a logging proxy inside run_session.py. pi's model `baseUrl` points at
  `http://127.0.0.1:<port>/v1beta`; the proxy forwards to generativelanguage.googleapis.com and saves
  every request body exactly as sent to `raw/requests/<call_id>.json`, the raw response to
  `raw/responses/`, and usage per call to `raw/proxy_calls.jsonl` (promptTokenCount,
  cachedContentTokenCount, candidates + thoughts). Same in every condition, and it also catches
  pi's compaction summary calls (tagged `purpose: compaction` by their system prompt). Headers are
  forwarded but never written; the key is checked absent from raw/ (grep) after the first test.
- Each call is tagged with the turn and thread the driver is on (turns are strictly sequential).
- AG's Gatherer calls go through LiteLLM directly (not the proxy) and are read from AG's call log
  (`raw/ag/home/logs/llm/*.json`, with `log_prompts = true`).

## 2026-11-17: step 2, AG findings

- `ag --gatherer <run_id> pi --mode rpc ...` works with `AG_HOME` and `AG_PYTHON` set; the harness
  command is `pi` found on PATH (the runner puts the local pi first). `[policies.llm] model =
  "gemini/gemini-3.8-flash"`.
- If AG cannot start (bad config), pi does not fail: AG reports `ag: off` through an RPC notify and pi
  runs without it. The runner treats that as a fatal error for the run.
- **Defaults plus `max_context_tokens = 100000` is refused at 59ca484**: "the budgets do not fit
  max_context_tokens=100000: sections 100000 + keep_pinned_tokens 5000 = 105000". The section budgets
  include two knobs CLAUDE.md did not list: `parallel_index_tokens` (10000) and `keep_so_far_tokens`
  (15000). Decision for the smoke run: `ag_default` = every budget scaled by 0.5, which is AG's own
  `within_limit` rule for shrinking its 200k defaults to a lower limit: min_context 7500,
  keep_pinned 2500, all_task 15000, parallel_index 5000, keep_temporal/task/previous 7500 each,
  keep_so_far 7500. Consequence: the planned `ag_tight` (relevance budgets 7500) equals this, so
  `ag_tight` is set to 3750 for those three for now. ASK TIM.
- Observed AG token counts (tiktoken) run about 1.5x below Gemini's count for this text (read 10:
  est 19096, real 28814). AG scales by the provider's count for its limit, so this is handled.

## 2026-11-17: step 2, trace export

- `trace/export.py` builds the TRACE.md layout from raw/. Notes on fields:
  - calls.jsonl: one row per Actor call including pi's compaction summary calls. Added two fields
    beyond TRACE.md: `purpose` ("agent" or "compaction") and `usd`. No TRACE.md field renamed.
  - contexts.jsonl: pi runs from the captured request (system prompt, tool definitions, each content;
    user messages matched to session turns in order, since short follow-ups recur verbatim in the
    script; assistant and tool items take the turn of the user message before them; a pi compaction
    summary is kind "summary" with label "compaction summary"). AG runs from the store (`_reads`,
    `_read_items`, `_made_items`, `_preambles`), paired with the agent calls in order. Item tokens:
    AG's own counts for AG; tiktoken cl100k for pi (about 0.74x Gemini's count). `total_tokens` is
    always Gemini's input count.
  - builds.jsonl `tasks`: AG's `tasks` table keeps only the latest segmentation, so `tasks` is filled
    for the last build of the run and null for earlier builds. To keep them all, a later version could
    snapshot the table after every turn.
  - gatherer_calls.jsonl turn: by the call's start time within the turn windows (builds run in the
    background, so a build's calls can land in the turn after the one that started it).
  - turns.jsonl `events`: "compaction" from pi's session file compaction entries (or a compaction
    call), "new_task" from `_boundaries`, "build"/"forced_build" (`_builds.reason = limit`), "error".

## 2026-11-17: step 2, smoke runs

All under `results/smoke/` (raw/ is local only). Commands:
`python run/run_session.py --condition <c> --session sessions/pilot_seed0 --max-turns <n> --results results/smoke [--rep 2]`,
then `venv/bin/python trace/export.py --run <dir>` and `grade/grade.py`.

| run | turns | actor $ | gatherer $ | total $ | peak actor context | compactions | AG builds |
|---|---|---|---|---|---|---|---|
| pi_compact_seed0_rep1 | 1-30 | 0.231 | 0 | 0.231 | 75,422 | 0 | |
| ag_default_seed0_rep1 | 1-30 | 0.262 | 0.232 | 0.494 | 71,530 | 0 | 2 (1 ready, 1 still running at the end) |
| pi_compact_seed0_rep2 | 1-60 | 0.609 | 0 | 0.609 | 100,272 | 1 (turn 55) | |

Earlier probes (two 2-turn pi tests, one 2-turn ag pi test, one 3-turn runner test) cost about $0.03.
countTokens calls are free. Total spend to end of step 2: about $1.37 of the $10 cap.

- Compaction confirmed: at turn 55 pi compacted at tokensBefore 100,272; the next call was 39,610
  tokens. Visible as a `compaction` entry in the pi session file, as `compaction_start/end`
  (reason "threshold") in the RPC stream, and as one proxy call with purpose "compaction"
  (61.9k in, 748 out). Exported to decisions.jsonl and turns.jsonl.
- Replaced system prompt under ag pi: confirmed in a captured request (c00032 of the AG run):
  systemInstruction is run/system_prompt.md plus pi's date and cwd lines. AG's gatherer guide and its
  sections arrive as user-role items after it.
- Grades (only deliverables due within the truncated script exist, so overall rates are not
  meaningful): 30-turn runs both wrote D01 and D02, all stated checks pass, both UNK fields null. 60-turn
  pi run wrote D01 to D09, every stated check passes, 3 UNK `contact_name` fields made up (D04, D06, D07).
- Calls per turn: about 1.1 to 1.2 (one reply, plus a short follow-up call after each file write).
  Implicit caching works: 88 to 92% of actor input tokens are billed at the cache rate.
- AG observations for step 3: intended visit switches in turns 1 to 30 are at 5, 8, 11, 13, 15, 19,
  22, 24, 28. AG asked the boundary question at turns 3, 4, 5, 19 to 24 only, and called new_task at
  5 and 24. No checks between 6 and 18 while build 1 was running (it took 107 s, segment effort high,
  and was adopted by reads from about turn 19). Because the open task since turn 5 stays verbatim,
  AG's context at turn 30 (71.5k) is close to pi's (75.4k).
- Gatherer spend by purpose (30 turns): boundary 9 calls, 218k input, $0.164; segment $0.015;
  subdivide $0.021; summarize $0.032. LiteLLM reports no cached tokens for Gatherer calls.

## 2026-11-17: step 2, session size

- Gemini countTokens on the user messages: all 145 turns = 144,034 tokens (244,288 chars, 1.70 chars per
  token; the generator's chars/4 estimate of 61k was low because CSV exports tokenize densely).
  First 30 turns 66,565, first 60 turns 93,835.
- Projection for a full pi_compact run: growth after turn 60 is slower (about 50k user tokens left over
  85 turns, plus about 300 tokens per reply), so the second compaction should land around turn 110 to
  120 and a third is unlikely. That meets "passes 100k at least twice", but only just. Not regenerating;
  asking Tim whether to raise `--report-rows` for margin.
