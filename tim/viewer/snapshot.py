"""Bundle runs into one self-contained HTML file (no server needed), for sharing or a quick look.
Exact requests and responses are left out (they hold the full session text and live in raw/).

  python viewer/snapshot.py --root results/smoke --out /tmp/viewer_snapshot.html
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

ap = argparse.ArgumentParser()
ap.add_argument("--root", action="append", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
serve.ROOTS[:] = [os.path.abspath(r) for r in a.root]
runs = serve.find_runs()
cards = [serve.run_card(k, v) for k, v in sorted(runs.items())]
for c in cards:
    c.pop("path", None)
data, deliv, session = {}, {}, None
for rid, d in runs.items():
    run = serve.jf(os.path.join(d, "run.json"), {})
    out = {"run": run, "summary": serve.jf(os.path.join(d, "summary.json"), {}), "cost": serve.jf(os.path.join(d, "cost.json"), {}),
           "grades": serve.jl(os.path.join(d, "grades.jsonl")), "attribution": serve.jl(os.path.join(d, "attribution.jsonl")),
           "coverage": serve.jl(os.path.join(d, "coverage.jsonl"))}
    for t in serve.TRACE_FILES:
        out[t] = serve.jl(os.path.join(d, "trace", t + ".jsonl"))
    data[rid] = out
    dd = os.path.join(d, "deliverables")
    deliv[rid] = {f[:-3]: open(os.path.join(dd, f)).read() for f in os.listdir(dd)} if os.path.isdir(dd) else {}
    session = session or serve.session_for(run)
snap = {"runs": cards, "data": data, "deliv": deliv, "session": session, "built": time.strftime("%Y-%m-%d")}
html = open(os.path.join(serve.HERE, "index.html")).read()
payload = json.dumps(snap).replace("</", "<\\/")
html = html.replace("<script>/*SNAPSHOT*/</script>", "<script>window.SNAPSHOT=" + payload + ";</script>")
open(a.out, "w").write(html)
print(a.out, f"{len(html)/1e6:.1f} MB, {len(cards)} runs")
