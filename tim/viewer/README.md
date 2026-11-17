# Trace viewer and attribution

Reads the normalized trace layout in `../TRACE.md`. Standard library only, runs locally.

```
python viewer/serve.py                      # scans tim/results/, http://127.0.0.1:8765
python viewer/serve.py --root results --root ~/other_runs
python analysis/attribute.py --session sessions/pilot_seed0 --all results   # writes attribution.jsonl per run
python viewer/make_synthetic.py             # fake runs in results/_synthetic to try it out
```

A run is any folder with `run.json`. New runs appear on Reload; the "Add" box adds another folder.

Tabs:
- **Runs**: one row per run with pass rate (overall, A, C, D, CTRL), made-up rate, the three costs, latency, retrieval vs usage misses. Tick runs to compare.
- **Run**: context size per turn with compaction and AG new-task and build markers and client visits shaded; accuracy dots at each checkpoint with a rolling pass rate; latency per turn (actor plus gatherer); cumulative cost. Click any chart to open the turn inspector: user message, tokens, cost, latency, AG boundary verdict and reason, build (ranking, adopted sections, stored tasks), what was in the context on the last call, and exact request on demand.
- **Checks**: every graded check, filterable by tag, outcome, placement and verdict. Detail shows where the fact was said, what the agent wrote, and highlights which messages in the request contain it.
- **Compare**: pass rate by check type, placement and fact age; accuracy vs spend; context size, rolling accuracy and cumulative cost overlays; why checks failed. Replicates of one condition can be averaged.

Raw request files (`raw/requests/`) are local only; without them the context contents still show but
the exact-request view and attribution are unavailable.
