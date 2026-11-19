# Long-session benchmark: does Actor-Gatherer beat a regular agent?

Status: step 1 done (generator, grader, pilot session). Steps 2 to 4 handed to Claude Code, see CLAUDE.md. Owner: Tim.

## 1. Question

When one person works with an assistant over a long session, giving related prompts that drift across
several workstreams, the agent degrades: compaction drops details, recent work crowds out older work,
and old instructions survive alongside new ones. Actor-Gatherer (AG) claims to fix this by rebuilding
the Actor's context for the current task from the whole history, before every model call.

We test where AG helps, where it hurts, and what it costs, against the same harness without AG.

## 2. Hypotheses in scope

| Tag | Failure in a regular agent | Why AG should help | Check |
|---|---|---|---|
| A | A fact stated early is dropped by compaction or buried by length | Past tasks are ranked back in by relevance to the current task | Fact appears correctly in a late deliverable |
| C | Returning to a workstream after others: the summary is dominated by recent work | Ranking is driven by the current task, not recency | Fact from the workstream's earlier visits appears correctly |
| D | A superseded fact: old and new versions both survive | Time-aware selection should prefer the latest | The new value appears and the old one does not. Risk test: AG may page the old one back in |
| E | Context and cost grow until compaction | Context stays bounded | Tokens and dollars per turn |
| CTRL | Fact is in the current prompt | Nothing to gain | Regression check: AG must not make these worse |
| UNK | A field is requested before its fact was ever stated | Nothing to page in | The right answer is null. A value counts as made up. Reported separately, not in the main pass rate |

Out of scope for the pilot: B (self-poisoning, weak fit with AG's design since the running task stays
verbatim) and F (environment changes picked up by pollers, planned as module 2).

## 3. Scenario: multi-client account manager

One account manager, one long scripted session, 4 client accounts. The user (the account manager)
asks the assistant for deliverables: renewal quotes, follow-up emails, meeting prep briefs, QBR
summaries, discount approvals. Facts about each client arrive over time, mostly inside the user's
messages and pasted call notes: budget ceilings, contract terms, contacts, tone preferences, banned
topics, promised concessions, deadlines. Some facts are later superseded.

Facts live only in the conversation. The agent has a working folder for writing deliverables, but
no CRM to look facts up in (that would make it a control, and environment changes are module 2).

### Session shape (driven by how AG detects tasks)

AG rebuilds context only when it decides a new task has started. From
`ag/ag_policies/proactive_context_manager/tools/boundary.py` and `policy.py`:

- A task is a run of consecutive turns serving one high-level objective. Follow-ups and refinements
  are the same task. When unsure, it answers "same task".
- It only asks once the context is over `min_context_tokens` (15k default) and after
  `boundary_cooldown` (2) user messages into the open task.
- Segmentation re-cuts the whole history at every build, so a missed boundary is found later.
- Over `max_context_tokens`, a build is forced regardless.

So the script is built from **visits**: 2 to 4 consecutive user messages about one client with one
objective (for example "Acme: draft the renewal quote", then two refinements). Visits switch client or
objective, which should read as a new task. A 1-message visit would never be checked and would be
absorbed into its neighbors, so every visit is at least 2 messages.

Two fact placements, tagged separately:
- **home**: the fact is stated during a visit to its own client.
- **embedded**: the fact is mentioned in passing during another client's visit ("btw, Acme called,
  their CFO is now Dana"). This is harder for task-based ranking and is realistic.

### Pilot size

Generated (`sessions/pilot_seed0`, seed 0): 4 clients, 48 visits (balanced across clients), 145 user messages, 32 deliverables,
163 checks: 129 on stated facts (A 86, C 80, D 14, CTRL 6, NEAR 5; home 102, embedded 27) and 34 UNK. User text is about 240k
characters (roughly 60k to 70k tokens, mostly pasted usage exports and email threads); with the agent's
replies and tool calls the actor history should pass 100k at least twice. Claude Code measures the
real size in the smoke test and regenerates with a larger `--report-rows` if needed.

Known limits of v0 text: fact sentences are crisp and templated, filler repeats across call notes.
An LLM paraphrase pass is a later improvement. CTRL has few checks; contrast comes mostly from the
age curve .

## 4. Generation

Python, deterministic, seeded:
1. Sample client profiles: about 10 facts each, typed (number, date, name, enum, banned phrase).
2. Build a schedule: visits, which facts are introduced where (home or embedded), which are
   superseded and when, which deliverables need which facts.
3. Render messages from templates. An optional LLM paraphrase pass rewrites wording only, then a
   validator confirms every planted value is still present verbatim.
4. Emit `session.jsonl` (the user messages, in order) and `checks.jsonl` (per deliverable:
   the required values, forbidden values, and tags A/C/D/CTRL with fact age and visits-since-last).

### Decisions (Nov 17)

- Unknown values: the format line asks for null for anything not yet told. The scripted user cannot
  answer clarifying questions, so asking is not an option in this design.
- Client wording rules (style checks): the request describes the topic in neutral words and never
  uses the banned word itself, so the check tests memory of the rule rather than conflict resolution
  between an old rule and a new instruction. Style checks are reported as their own slice.
- System prompt: pi's default coding prompt is replaced in every condition by `run/system_prompt.md`,
  a short account-manager-assistant prompt. Tools stay the same.
- Models: a Gemini Flash-tier model for both Actor and Gatherer.

## 5. Grading

No probe questions. Deliverables are written to files in a fixed format: a JSON header block
(client, amounts, dates, contact) plus free text. Checks are code only:
- required value present in the right field or text
- forbidden value absent (superseded value, banned phrase)
- numbers within the stated ceiling

## 6. Conditions

Same Actor model, same scripted session, same 100k context ceiling everywhere.

| # | Condition | Notes |
|---|---|---|
| 1 | Pi, full context, no compaction (`pi_full`) | Context-rot reference. The model's real window (1,048,576 tokens for gemini-3.8-flash); the whole session (~150k tokens) fits, so it runs on the full session, 1 run |
| 2 | Pi with native compaction at 100k | The main baseline |
| 3 | Pi, one session per client (`pi_per_client`) | The "separate threads" workaround people use. Embedded facts land in the wrong client's thread, as they would for a real user |
| 4 | AG + Pi, `ag_half`: `max_context_tokens = 100000` with every AG budget halved (AG's defaults sum to 105k and do not fit under 100k; halving is AG's own `within_limit` rule) | Main treatment |
| 5 | AG + Pi, knob variants (`ag_quarter` first: as `ag_half` with `keep_*_relevant_tokens` and `keep_previous_task_tokens` at 3750) | For the accuracy-vs-spend curve: `min_context_tokens`, `keep_*_relevant_tokens`, `summarize` |

2 runs per condition for the pilot.

## 7. Metrics

- Pass rate overall and by tag (A, C, D, CTRL), and home vs embedded. Style checks as their own slice.
- Made-up rate on UNK fields.
- Pass rate vs fact age (turns since stated) and vs turn index: the degradation curves.
- Cost reported as three numbers: Actor (Pi) tokens and dollars, Gatherer tokens and dollars,
  and the combined total. Pi-only conditions have only the first.
- Latency per turn.
- AG diagnostics: where it cut tasks vs our intended visit boundaries, and what it paged in for
  each failed check.

## 8. Process and gates

| Step | Where | Output | Gate |
|---|---|---|---|
| 0 | here | This design | Tim approves |
| 1 | here | Generator, grader, a sample session | Tim reads the sample session |
| 2 | laptop (Claude Code) | Smoke test: Pi + Gemini runs; Pi compaction threshold settable; AG proxies Pi on Gemini; costs logged separately | All four confirmed |
| 3 | laptop | Granularity check: run AG's segmentation and boundary check on the scripted messages alone, compare cuts to intended visits | Cuts mostly match. If not, adjust visit design before spending on runs |
| 4 | laptop | Pilot runs, traces saved under `tim/results/` | Runs complete |
| 5 | here | Grading, plots, short report | Bring to lab meeting |

## 9. Open items

- Which Gemini model for the Actor and for the Gatherer (they can differ; using the same is simpler to explain).
- Spend cap for the pilot.
- Pi's compaction setting name and whether its summary prompt can be logged.
- Whether condition 1 is worth including or replaced by reporting where condition 2 first compacts.
