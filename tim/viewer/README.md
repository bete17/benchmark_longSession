# Trace viewer and attribution

Reads the normalized trace layout in `../TRACE.md`. Standard library only, runs locally.

```
python viewer/serve.py                      # scans tim/results/, http://127.0.0.1:8765
python viewer/serve.py --root results --root ~/other_runs
python analysis/attribute.py --session sessions/pilot_seed0 --all results   # writes attribution.jsonl per run
python viewer/make_synthetic.py             # fake runs in results/_synthetic to try it out
python viewer/snapshot.py --root results/smoke --out /tmp/viewer.html   # one shareable file, no server (no raw requests)
```

A run is any folder with `run.json`. New runs appear on Reload; the "Add" box adds another folder.

Tabs:
- **Runs**: one row per run with pass rate (overall, A, C, D, CTRL), made-up rate, the three costs, latency, retrieval vs usage misses. Tick runs to compare.
- **Run**: context size per turn with compaction and AG new-task and build markers and client visits shaded; accuracy dots at each checkpoint with a rolling pass rate; latency per turn (actor plus gatherer); cumulative cost. Click any chart to open the turn inspector: user message, tokens, cost, latency, AG boundary verdict and reason, build (ranking, adopted sections, stored tasks), what was in the context on the last call, and exact request on demand.
- **Checks**: every graded check, filterable by tag, outcome, placement, age bin and verdict; shows the age bin and a "buried" chip for facts inside pasted notes. Detail shows where the fact was said, what the agent wrote, and highlights which messages in the request contain it.
- **Side by side**: one turn across the ticked runs: each manager's decision, what each model saw (verbatim turns vs AG-made items), checks, and the exact request and response.
- **Guide**: what is measured and how to read it.
- **Compare**: at the top, accuracy over the session at fixed fact age (fresh, medium, old; one line per condition or run); then pass rate by check type, placement and fact age; accuracy vs spend; context size, rolling accuracy and cumulative cost overlays; why checks failed. Replicates of one condition can be averaged.

Raw request files (`raw/requests/`) are local only; without them the context contents still show but
the exact-request view and attribution are unavailable.

Age bins: `age_bin` and `session_bin` are read from each grade row when present; otherwise the viewer computes them from `turn` and `age_turns` using the `AGE_EDGES` constant in index.html (fresh 2-10, medium 15-45, old 60+, else other; UNK rows have no age). Chart points with fewer than 3 checks are drawn hollow and not joined.
