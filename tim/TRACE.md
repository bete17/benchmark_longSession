# Trace format

Every run, in every condition, produces the same normalized trace, so one viewer and one set of
analysis scripts work across all runs. `run/run_session.py` saves raw material; `trace/export.py`
turns raw material into the normalized files below; the viewer and analysis read only the normalized
files.

## Folder layout

```
tim/results/<run_id>/                  run_id = <condition>_seed<k>_rep<r>, e.g. ag_half_seed0_rep1
  run.json                             what was run (committed)
  deliverables/                        what the agent wrote (committed)
  trace/                               normalized trace (committed)
    turns.jsonl
    calls.jsonl
    contexts.jsonl
    decisions.jsonl
    builds.jsonl                       AG runs only
    gatherer_calls.jsonl               AG runs only
  grades.jsonl, summary.json           from grade/grade.py (committed)
  attribution.jsonl                    from analysis: was each checked fact in context? (committed)
  cost.json                            actor_usd, gatherer_usd, total_usd (committed)
  raw/                                 local only, gitignored
    pi/                                pi session file(s), one per thread
    ag/                                a copy of the gatherer's store (SQLite), server.log, call log
    requests/<call_id>.json            the exact request each Actor model call sent (see below)
```

`turn` everywhere is the session's user-message number from `session.jsonl` (1 to 149 for pilot_v2_seed0, 1 to 145 for pilot_seed0).

## run.json

`run_id, condition, seed, rep, session_dir, actor_model, gatherer_model, ag_commit, pi_version,
ceiling_tokens, system_prompt_sha, started_at, finished_at, status (complete|partial|failed),
turns_completed, errors` (list of `{turn, where, message}`), plus the condition's AG options.

## trace/turns.jsonl (one row per user turn)

`turn, visit, client, thread` (client name for pi_per_client, else "main"), `started_at, seconds`
(wall time until the agent finished the turn, tool calls included), `actor_calls` (count),
`actor_input_tokens, actor_output_tokens, actor_cache_tokens` (summed over the turn's calls),
`context_tokens` (largest Actor input on the turn), `gatherer_input_tokens, gatherer_output_tokens,
gatherer_seconds`, `actor_usd, gatherer_usd`, `events` (list of short tags: "compaction",
"new_task", "build", "forced_build", "error").

## trace/calls.jsonl (one row per Actor model call)

`call_id, turn, thread, index_in_turn, started_at, seconds, input_tokens, output_tokens,
cache_tokens, stop_reason, tool_calls` (names), `request_file` (raw/requests/<call_id>.json).

The request file is the exact payload the model received (system prompt and messages), captured the
same way in every condition. Options: a pi extension on the `context` event registered after AG's,
or a logging proxy in front of the Gemini API. Whichever is used, record it in RUNLOG.md.

## trace/contexts.jsonl (one row per Actor model call: what was in front of the model)

`call_id, turn, total_tokens, items`: an ordered list, each item
`{kind, origin_turn, role, tokens, label}`:
- `kind`: "message" (an original message, verbatim), "summary" (a compaction summary or an AG-made
  item), "system".
- `origin_turn`: the user turn the item came from (null for summaries and system).
- `label`: for AG-made items, the item's name (e.g. a section name); for pi, "compaction summary".

How to fill it:
- AG runs: from the store. `_reads` has one row per call (`est_tokens`, `real_tokens`, `basis` = the
  build); `_read_items` (or the `read_log` view) lists what that read returned in order, each either a
  stored event (`row_id` into `events`) or a made item (`made_id` into `_made_items`, exact text).
- pi runs: from the pi session file (compaction entries and the messages kept after them), checked
  against the request file.

## trace/messages.jsonl (one row per user turn: the conversation as it happened)

`turn, thread, user` (the user text), `steps`: the agent's side of the turn in order, each
`{call_id, kind, name, text}` where kind is "text" (reply text), "thinking" (if the model returned
thoughts), "tool_call" (`name` is the tool, `text` its arguments as JSON, e.g. a write's path and
content), or "tool_result" (`name` the tool, `text` the result, truncated to 2000 chars). From the pi
session file (or the captured responses). Committed: it is what the viewer's conversation view shows
when raw/ is not available.

## trace/decisions.jsonl (one row per decision the context manager made)

`turn, kind, detail`:
- AG `boundary`: from `_boundaries` (`new_task` 1 or 0, `why`), one per user message checked.
- AG `build`: from `_builds` (`build_id, status, seconds, through_turn, open_from_turn, forced`).
- pi `compaction`: `thread, tokens_before, tokens_after, summary_chars`.

## trace/builds.jsonl (AG only, one row per build)

`build_id, turn, status, seconds, open_from_turn, tasks` (from the `tasks` table: `index, name,
description, first_turn, last_turn, tokens, retention` where retention is what the task established),
`ranking` (earlier tasks, most relevant first), `sections` (name and tokens of the made items the next
read adopted). `report.describe()` in `ag/ag_policies/proactive_context_manager/report.py` already
computes most of this.

## trace/gatherer_calls.jsonl (AG only)

`turn, purpose` (boundary, segment, rank, summarize, ...), `model, input_tokens, output_tokens,
seconds, usd`. From AG's call log (`ag/ag_tools/llm/calllog.py`).

## coverage.jsonl (from analysis/coverage.py, one row per turn)

`turn, call_id, n, verbatim, paraphrased, absent, coverage, lost`: of the facts said so far (current
value of each client field), how many are in the turn's last agent request. Shows whether compaction or
AG keeps facts in view, independent of grading.

## attribution.jsonl (from analysis, one row per graded check)

For each check: the call that wrote the deliverable, and whether the fact's source was in that call's
request: `verbatim` (the exact source sentence from `facts.json` is in the request text),
`paraphrased` (the value and the client name both appear within one item, e.g. inside a summary), or
`absent`. Combined with the grade:
- fail + absent: **retrieval miss** (compaction dropped it, AG did not page it in, wrong thread)
- fail + verbatim/paraphrased: **usage miss** (it was there, the model did not use it)
- pass + absent: the agent got it right without the fact in view (guess or leak; inspect)

## Which call wrote a deliverable

`analysis/attribute.py` takes, for a check on turn N, the last call of turn N whose `tool_calls` include
a write tool (name contains write, edit or create), else the last call of that turn. Keep tool names
in `calls.jsonl` as the harness reports them (for pi: `write`, `edit`).

## Mapping AG rows to turns

AG's `events` table numbers every stored item by `row_id`. The n-th `user_message` event in the
channel is turn n. Build this map once per run in the exporter and convert every row reference
(`first_row`, `last_row`, `open_from_row`, `_boundaries.row_id`) to turns.
