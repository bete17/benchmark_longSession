# Long-session benchmark: does Actor-Gatherer beat a regular agent?

Status: step 1 done (generator, grader, pilot session). Generator v2 (fixed-age check schedule) added Nov 19 2026. Steps 2 to 4 handed to Claude Code, see CLAUDE.md. Owner: Tim.

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
| S | The agent degrades as the session gets long (more context, more compactions), independent of how old any fact is | Context stays bounded, so late turns should look like early ones | Accuracy on fresh facts (2 to 10 turns old) by session position: if it falls late in the session, length itself hurts. Fact-age effects are read separately, at a fixed session position |
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

### Pilot size (v1, `sessions/pilot_seed0`)

Superseded for the main runs by schedule v2 (next section). Kept for the step 2 smoke runs.


Generated (`sessions/pilot_seed0`, seed 0): 4 clients, 48 visits (balanced across clients), 145 user messages, 32 deliverables,
163 checks: 129 on stated facts (A 86, C 80, D 14, CTRL 6, NEAR 5; home 102, embedded 27) and 34 UNK. User text is about 240k
characters (roughly 60k to 70k tokens, mostly pasted usage exports and email threads); with the agent's
replies and tool calls the actor history should pass 100k at least twice. Claude Code measures the
real size in the smoke test and regenerates with a larger `--report-rows` if needed.

Known limits of v0 text: fact sentences are crisp and templated, filler repeats across call notes.
An LLM paraphrase pass is a later improvement. CTRL has few checks; contrast comes mostly from the
age curve .

### Schedule v2: fixed-age checks (decision, Nov 19 2026)

Problem with v1: fact age and session position were confounded. 25 of 42 facts were planted in the first
45 turns, so late checkpoints only tested old facts (median fact age 22 at turns 1 to 30, 32 at 31 to 60,
53 at 61 to 90, 77 at 91 to 120, 98 at 121 to 145; almost no fact under 20 turns old after turn 60) and
only 4 checks fell in turns 1 to 30. "The agent degrades as the session gets long" could not be told apart
from "late checks ask about older facts". A smoke run also showed pi's compaction summary kept all 27 facts
said so far, so v1 was easy for a good summarizer.

Decision: `generate.py --schedule v2` (the default; `--schedule v1` reproduces the old session byte for byte).
- Facts are planted throughout the session: 80 to 100 statements, at most 40% in the first third, at least 30
  supersessions spread over all thirds, 25 to 30% embedded in another client's visit.
- About 30% of facts are buried: a bullet inside pasted call notes, or a line in a forwarded client email
  (`form: "buried"` in facts.json and checks.jsonl), the rest are stated outright (`form: "sentence"`).
  Summaries tend to keep headline numbers and drop fine print, which is the point.
- 38 deliverables, the first by turn 11, spread evenly to the end (about 8 per 30-turn window). Every
  deliverable header holds fields of three age bins (age = turns between the statement of the current value
  and the checkpoint):
  - fresh, 2 to 10 turns: at least one per deliverable. Stated earlier in the same visit (not in the request
    message) or in the immediately preceding visit.
  - medium, 15 to 45 turns: at least one per deliverable from turn 20.
  - old, 60+ turns: at least one per deliverable from turn 66.
  - other (0 to 1, 11 to 14, 46 to 59): allowed for the remaining fields.
- Each check carries `age_bin` (null for UNK), `session_bin` (0-based 30-turn window of the checkpoint turn)
  and `form`. CTRL stays about 1 deliverable in 4, UNK stays. The grader reports `by_age_bin`,
  `by_session_bin`, `by_session_bin_x_age_bin` and `by_form`, leaving out CTRL and UNK.
- Reading it: S is the fresh column across session bins. Fact-age effects are the age columns within one
  session bin. `python gen/report.py --session sessions/pilot_v2_seed0` prints the counts and confirms the
  schedule.
- Size is held near v1 so cost does not grow. The v2 header types are wider than v1 (6 or 7 fields each) so
  every type can carry all three ages.
- Balance inside each age bin (added Nov 19, after the first v2 build). The first v2 session had a confound: old
  checks (60+ turns) fell on fields that never change (currency, term, concession reference, style rule) and on
  facts said in the client's own visit, while fresh checks fell on changing fields and on embedded facts. Stable
  headline fields are what summaries keep, so old-vs-fresh would have partly measured stable-vs-changing field
  (first build: supersedable share 88% fresh, 69% medium, 16% old; embedded 40%, 32%, 9%). Now the generator
  balances, inside each of fresh, medium and old (CTRL and UNK left out; style checks count as stable):
  1. Field group: share of checks on SUPERSEDABLE fields (contact_name, cc_name, seats, discount_pct,
     renewal_deadline) differs by at most 15 points between the three bins, and every field appears in at least
     two of the three bins.
  2. Placement: share of checks whose fact was said in another client's visit (embedded) differs by at most 10
     points between bins. Level is about 30 to 36%, a little above the 25 to 30% of statements overall, because
     embedded statements tend to be the current value for more checks (the previous-visit fresh facts are
     embedded by construction).
  3. Form: share of checks whose fact was buried differs by at most 10 points between bins (about 30% each).
  How: after the statement plan, a simulated-annealing pass (`anneal_plan`, cost `plan_cost`) moves, adds and
  removes statements under every slot rule. Supersedable fields may stay unchanged for long stretches (so they
  can be old) and stable fields may be first stated late (so they can be fresh or medium; the checks before
  that are UNK). Which statements are buried is chosen after the plan (`choose_buried`) to equalise the buried
  share per bin. The generator retries with derived seeds until `balance_problems` is empty on the real checks
  (with a 15% margin on each limit). `gen/report.py --strict` and `gen/test_generate.py` (seeds 0 and 1) check
  the same criteria. The cost of balance: supersessions dropped from 57 to 49 and D checks from 89 to 75.
- Still read `by_field` next to the age bins when a result looks odd; balance removes the systematic confound,
  not the small per-field counts (a field has 0 to 14 checks in a bin).

`sessions/pilot_v2_seed0` (seed 0): 4 clients, 49 visits (2 to 4 messages), 150 user messages, 38 deliverables,
242 checks: 200 on stated facts (A 73, C 115, D 75, CTRL 10, NEAR 18; home 134, embedded 66) and 42 UNK; 84 fact
statements, 49 of them supersessions, 25 embedded, 24 buried; 248k user characters (v1: 244k, 145 messages).
Main checks by age bin (CTRL and UNK left out): fresh 50, medium 60, old 47, other 33. Share on supersedable
fields 74%, 73%, 70% (fresh, medium, old); embedded 36%, 35%, 34%; buried 30%, 32%, 32%.

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
- Compaction ceiling (100k now): to be decided after a free "facts still in view" check (analysis/coverage.py) on a short v2 run. If a good summarizer still keeps every fact, lower the ceiling.
- Whether condition 1 is worth including or replaced by reporting where condition 2 first compacts.
